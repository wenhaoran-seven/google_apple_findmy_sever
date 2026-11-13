"""Independent persisted device schedules, with bounded concurrency through one account."""
import asyncio
import time
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import HTTPException
from findmy import UnauthorizedError, InvalidCredentialsError, InvalidStateError

from .polling_settings import settings, seconds, stagger
from .account import AppleAuthenticationError
from .mac_anisette import AppleTransportError


def now():
    return datetime.now(timezone.utc).timestamp()


def default_task(device_id):
    return {"enabled": False, "device_id": device_id, "interval_seconds": 300,
            "next_run": None, "last_run": None, "last_count": None,
            "error": None, "failures": 0}


class AutoFetch:
    def __init__(self, service):
        self.service = service
        saved = service.vault.read("schedule", {})
        if saved.get("version") == 2:
            self.tasks = saved["tasks"]
        else:
            device_id = saved.get("device_id")
            self.tasks = {device_id: {**default_task(device_id), **saved}} if device_id else {}
        self.cooldown_until = saved.get("cooldown_until", 0)
        self.running_devices = {}
        self.inflight = {}
        self.max_concurrency = service.query_concurrency
        self.revisions = {}
        self.wake = asyncio.Event()
        self.worker = None
        if saved and saved.get("version") != 2:
            self.save()

    def sync_settings(self):
        value=settings()
        if getattr(self,'_polling_revision',None)==value['revision']:return
        interval=value['interval_seconds']
        for ident,task in self.tasks.items():
            changed=task['interval_seconds']!=interval
            task['interval_seconds']=interval
            if changed and task['enabled']:
                due=now()+stagger(ident,interval)
                task['next_run']=max(due,task.get('next_run') or 0) if task.get('failures') else due
        self.save();self._polling_revision=value['revision']

    def save(self, tasks=None):
        self.service.vault.write("schedule", {"version": 2,
            "tasks": self.tasks if tasks is None else tasks, "cooldown_until": self.cooldown_until})

    def status(self):
        rows = []
        for device_id, item in self.service.devices.items():
            task = self.tasks.get(device_id, default_task(device_id))
            running = device_id in self.running_devices
            due = max(task["next_run"] or 0, self.cooldown_until) if task["enabled"] else None
            state = "running" if running else "paused" if not task["enabled"] else (
                "retrying" if task["error"] or self.cooldown_until > now() else
                "queued" if due <= now() else "waiting")
            history = getattr(self.service, "location_cache", {}).get(device_id, {})
            rows.append({**task, "device_name": item["name"], "configured": device_id in self.tasks,
                         "running": running, "state": state, "next_run": due,
                         "started_at": self.running_devices.get(device_id),
                         "latest_location": next(iter(history.get("reports", [])), {}).get("timestamp"),
                         "empty_count": history.get("empty_count", 0), "history_count": history.get("count", 0)})
        return {"tasks": rows, "running_device_id": next(iter(self.running_devices), None),
                "running_device_ids": list(self.running_devices), "max_concurrency": self.max_concurrency,
                "cooldown_until": self.cooldown_until}

    def configure(self, device_id, interval_seconds, enabled):
        self.service.device(device_id)
        if enabled and not self.service.status()["location_ready"]:
            raise HTTPException(409, "请先登录并取得位置查询权限，再启动自动拉取。")
        updated = {**self.tasks.get(device_id, default_task(device_id)),
                   "interval_seconds": seconds(), "enabled": enabled,
                   "next_run": now() if enabled else None, "error": None, "failures": 0}
        tasks = {**self.tasks, device_id: updated}
        self.save(tasks)
        self.tasks = tasks
        self.revisions[device_id] = self.revisions.get(device_id, 0) + 1
        self.wake.set()
        return self.status()

    def pause(self, device_id=None):
        # No device is used only internally by logout to pause every task.
        if device_id is not None:
            self.service.device(device_id)
        targets = list(self.tasks) if device_id is None else [device_id]
        tasks = dict(self.tasks)
        for target in targets:
            tasks[target] = {**tasks.get(target, default_task(target)), "enabled": False, "next_run": None}
        self.save(tasks)
        self.tasks = tasks
        for target in targets:
            self.revisions[target] = self.revisions.get(target, 0) + 1
        self.wake.set()
        return self.status()

    def remove(self, device_id):
        """Remove a device's polling task without deleting its saved reports."""
        self.service.device(device_id)
        tasks = dict(self.tasks)
        tasks.pop(device_id, None)
        self.save(tasks)
        self.tasks = tasks
        self.revisions[device_id] = self.revisions.get(device_id, 0) + 1
        self.wake.set()

    def start(self):
        self.worker = asyncio.create_task(self.loop())

    async def stop(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        # Drain active queries before closing the account or its ADI provider.
        if self.inflight:
            await asyncio.gather(*list(self.inflight.values()), return_exceptions=True)

    async def run_once(self, device_id):
        revision = self.revisions.get(device_id, 0)
        async with self.service.query_slot(device_id):
            task = self.tasks.get(device_id)
            if (not task or not task["enabled"] or revision != self.revisions.get(device_id, 0)
                    or self.cooldown_until > now()):
                return
            self.running_devices[device_id] = now()
            started = time.monotonic()
            error, count, pause, rate_limited = None, None, False, False
            try:
                result = await self.service.fetch_locations(device_id)
                count = result["last_fetch_count"]
            except (HTTPException, UnauthorizedError, InvalidCredentialsError,
                    InvalidStateError, AppleAuthenticationError):
                error = "设备或账号授权不可用，任务已暂停。请检查设备和登录状态后重新启动。"
                pause = True
            except AppleTransportError as exc:
                rate_limited = exc.status == 429
                error = "Apple 限制请求频率，所有自动任务至少等待 15 分钟。" if rate_limited else "Apple 服务暂时不可用，将延后重试。"
            except Exception:
                error = "拉取失败，请检查网络；任务将延后重试。"
            finally:
                self.running_devices.pop(device_id, None)
            if rate_limited:
                self.cooldown_until = max(self.cooldown_until, now() + 900)
            if revision != self.revisions.get(device_id, 0):
                self.save()
                return
            failures = task["failures"] + 1 if error else 0
            interval = seconds()
            delay = max(interval, min(interval * 2 ** min(failures, 6), 3600), 900 if rate_limited else 0)
            self.tasks[device_id] = {**task, "last_run": now(), "last_count": count, "error": error,
                "failures": failures, "enabled": not pause, "next_run": None if pause else now() + delay,
                "last_duration_seconds": round(time.monotonic() - started, 3)}
            self.save()

    async def dispatch(self, device_id):
        revision = self.revisions.get(device_id, 0)
        try:
            await self.run_once(device_id)
        except Exception:
            # Do not recreate deleted tasks or overwrite a concurrent user edit.
            if device_id in self.tasks and revision == self.revisions.get(device_id, 0):
                self.tasks[device_id].update(enabled=False, next_run=None,
                    error="任务保存失败，已暂停。请检查本地磁盘。")
        finally:
            self.inflight.pop(device_id, None)
            self.wake.set()

    async def loop(self):
        while True:
            self.sync_settings()
            self.wake.clear()
            pending = sorted((t for t in self.tasks.values()
                              if t["enabled"] and t["device_id"] not in self.inflight),
                             key=lambda t: t["next_run"] or 0)
            delay = None
            if len(self.inflight) < self.max_concurrency and pending:
                delay = max(0, (pending[0]["next_run"] or 0) - now(), self.cooldown_until - now())
                if delay == 0:
                    for task in pending:
                        if len(self.inflight) >= self.max_concurrency or (task["next_run"] or 0) > now():
                            break
                        device_id = task["device_id"]
                        self.inflight[device_id] = asyncio.create_task(self.dispatch(device_id))
                    # Let queries enter their slots before recomputing the queue.
                    await asyncio.sleep(0)
                    continue
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=min(5,delay) if delay is not None else 5)
            except TimeoutError:
                pass
