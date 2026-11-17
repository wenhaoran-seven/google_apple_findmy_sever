"""Async IPC to the single account-specific FCM receiver process."""
import asyncio
import json
import sys
import time
from store import ROOT


class PoolError(RuntimeError):
    def __init__(self, message, rate_limited=False, reset_connection=False):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.reset_connection = reset_connection


class LocationPool:
    def __init__(self):
        self.process = None
        self.reader = None
        self.ready = None
        self.pending = {}
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.restart_after = 0

    async def _read(self, process):
        try:
            while line := await process.stdout.readline():
                data = json.loads(line)
                future = self.ready if 'ready' in data else self.pending.get(data.get('id'))
                if future is None or future.done():
                    continue
                if data.get('ok', data.get('ready', False)):
                    future.set_result(data.get('data'))
                else:
                    future.set_exception(PoolError(data.get('error', 'Google 推送进程失败'), data.get('rate_limited', False), data.get('reset_connection', False)))
        except (ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            for future in [self.ready, *self.pending.values()]:
                if future is not None and not future.done():
                    future.set_exception(PoolError('Google 共享推送通道已退出，本次查询未完成'))

    async def _close(self):
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        if self.reader is not None:
            await asyncio.gather(self.reader, return_exceptions=True)
        self.process = self.reader = self.ready = None

    async def close(self):
        async with self.start_lock:
            await self._close()

    async def start(self):
        async with self.start_lock:
            if self.restart_after > time.monotonic():
                raise PoolError('Google 推送通道初始化失败，稍后自动重试，请检查网络与授权')
            if self.process is not None and self.process.returncode is None and self.reader and not self.reader.done():
                return
            await self._close()
            self.ready = asyncio.get_running_loop().create_future()
            self.process = await asyncio.create_subprocess_exec(sys.executable, str(ROOT/'worker.py'), 'locate-pool',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, cwd=ROOT, limit=8*1024*1024)
            self.reader = asyncio.create_task(self._read(self.process))
            try:
                # Registration is bounded at 50s, MCS connection/retry at 60s.
                await asyncio.wait_for(self.ready, 120)
            except BaseException:
                await self._close()
                self.restart_after = time.monotonic() + 60
                raise

    async def query(self, job_id, device_id):
        # Only read-only location requests are retried. The failed process is
        # closed in _query_once before a fresh receiver can accept this job.
        for attempt in range(2):
            try:
                return await self._query_once(job_id, device_id)
            except PoolError as error:
                if not error.reset_connection or attempt or self.restart_after > time.monotonic():
                    raise

    async def _query_once(self, job_id, device_id):
        await self.start()
        process = self.process
        future = asyncio.get_running_loop().create_future()
        self.pending[job_id] = future
        try:
            async with self.write_lock:
                process.stdin.write((json.dumps({'id':job_id, 'device_id':device_id})+'\n').encode())
                await process.stdin.drain()
            # Allow one bounded connection retry for token and query, then FCM.
            return await asyncio.wait_for(future, 250)
        except PoolError as error:
            if error.reset_connection:
                async with self.start_lock:
                    # Several queries may report the same disconnect. An old
                    # failure must never kill a replacement account receiver.
                    if self.process is process:
                        await self._close()
            raise
        except TimeoutError:
            async with self.start_lock:
                if self.process is process:
                    await self._close()
            raise PoolError('Google 共享推送查询超时，通道已重置；这不等同于返回 0 条位置') from None
        finally:
            self.pending.pop(job_id, None)
