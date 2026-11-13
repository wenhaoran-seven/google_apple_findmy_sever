import asyncio
import csv
import io
import json
import os
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from store import Store, ROOT
from keys import new_device, firmware_header, burn_command, burn_config, validate_pubkey, DEFAULT_CID
from geocoding import ReverseGeocoder, GeocodeError
from renewal_policy import excluded_eids, is_excluded
from location_pool import LocationPool, PoolError
from public_web import PUBLIC_ORIGINS
from unified_tracks import install as install_unified_tracks
from polling_settings import seconds as polling_seconds, sync_google, install as install_polling
from central_locations import backfill as central_backfill, persist as central_persist
SOURCE=os.environ.get('FINDMY_SOURCE','google2')
UNIFIED_MAP_ORIGIN = os.environ.get("FINDMY_MAP_ORIGIN", "http://localhost:8765")


def public_device(d, store, exclusions=None, stats=None):
    if stats is None:
        history = store.get('history:'+d['id'], [])
        count, latest = len(history), max((p['timestamp'] for p in history), default=None)
    else:
        count, latest = stats.get(d['id'], (0, None))
    if exclusions is None:
        exclusions = excluded_eids(store)
    return {k: v for k, v in {**d, 'renewal_excluded': is_excluded(d, exclusions), 'history_count': count, 'latest_location': latest,
                              'stale': bool(latest and time.time()-latest > 86400)}.items() if k != 'eik_hex'}


def merge_history(old, points):
    # Response metadata must not turn repeated coordinates/reports into new points.
    metadata = {'unknown_11', 'unknown_11_received_at'}
    def key(point):
        return json.dumps({k: v for k, v in point.items() if k not in metadata},
                          sort_keys=True, separators=(',', ':'))
    unique = {}
    def merge(point):
        identity = key(point)
        existing = unique.get(identity)
        if existing is None:
            unique[identity] = dict(point)
        elif point.get('unknown_11_received_at', 0) >= existing.get('unknown_11_received_at', 0):
            unique[identity] = {**existing, **point}
    for point in old:
        merge(point)
    previous = len(unique)
    for point in points:
        merge(point)
    return sorted(unique.values(), key=lambda p: p['timestamp'], reverse=True), len(unique)-previous


class Service:
    def __init__(self):
        self.store = Store()
        self.geocoder = ReverseGeocoder(self.store)
        for device in self.store.all('device:'):
            self.store.save_device_with_sn(device)
        self.token = secrets.token_urlsafe(32)
        self.queue = asyncio.Queue(maxsize=20)
        self.pending = set()
        self.running = None
        self.running_jobs = {}
        self.query_concurrency = 10
        self.location_pool = LocationPool()
        self.query_cooldown = self.store.get('query_cooldown', 0)
        self.maintenance = False
        self.process = None
        for job in self.store.recent_jobs(unfinished=True):
            if job['state'] in ('queued', 'running'):
                job.update(state='interrupted', error='服务重启中断了任务，请核对状态后再试', finished_at=time.time())
                self.store.put('job:'+job['id'], job)
                if job['operation'] == 'delete':
                    d = self.store.get('device:'+job['device_id'])
                    if d:
                        d.update(enabled=False, delete_state='failed', delete_error='服务重启中断删除，请重试以核对 Google 状态')
                        self.store.put('device:'+d['id'], d)

    def ready(self):
        return all(self.store.get('auth:'+k) for k in ('username', 'aas_token', 'owner_key', 'fcm_credentials'))

    def device(self, id):
        d = self.store.get('device:'+id)
        if not d:
            raise HTTPException(404, '设备不存在')
        return d

    def enqueue(self, op, id=None, automatic=False):
        if self.maintenance:
            raise HTTPException(409, '正在更新账号授权，请稍后重试')
        if op == 'locate' and self.query_cooldown > time.time():
            raise HTTPException(429, 'Google 查询限流冷却中，请稍后重试')
        if not self.ready():
            raise HTTPException(409, '请先完成本机 Google 授权并导入')
        if (op, id) in self.pending:
            raise HTTPException(409, '相同任务正在排队或运行')
        if self.queue.full():
            raise HTTPException(409, '队列已满，请等待当前任务完成')
        d = None
        if id:
            d = self.device(id)
            if d.get('delete_state') and op != 'delete':
                raise HTTPException(409, '此设备正在删除或等待确认注销，请先完成删除核验')
        if op == 'delete' and (self.running or self.pending):
            raise HTTPException(409, '请等当前任务结束后再删除设备')
        job = {'id': str(uuid.uuid4()), 'operation': op, 'device_id': id,
               'device_name': d['name'] if d else None,
               'state': 'queued', 'created_at': time.time(), 'error': None, 'automatic': automatic}
        if op == 'delete':
            self.store.queue_deletion(d, job)
        else:
            self.store.put('job:'+job['id'], job)
        self.pending.add((op, id))
        self.queue.put_nowait(job)
        return job

    async def worker(self):
        active = set()
        try:
            while True:
                if len(active) >= self.query_concurrency:
                    done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    await asyncio.gather(*done, return_exceptions=True)
                job = await self.queue.get()
                if job['operation'] != 'locate':
                    # Administrative operations must not overlap queries or a
                    # second FCM receiver using the same account credentials.
                    if active: await asyncio.gather(*active, return_exceptions=True)
                    active.clear()
                    await self.location_pool.close()
                    await self.execute_job(job)
                else:
                    done = {t for t in active if t.done()}
                    if done: await asyncio.gather(*done, return_exceptions=True)
                    active -= done
                    active.add(asyncio.create_task(self.execute_job(job)))
        finally:
            for task in active: task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            await self.location_pool.close()

    async def execute_job(self, job):
        if job.get('automatic') and not self.device(job['device_id']).get('enabled'):
            job.update(state='skipped', finished_at=time.time(), error=None)
            self.store.put('job:'+job['id'],job)
            self.pending.discard((job['operation'],job['device_id']));self.queue.task_done()
            return
        self.running_jobs[job['id']] = job
        self.running = next(iter(self.running_jobs))
        job.update(state='running', started_at=time.time())
        self.store.put('job:'+job['id'], job)
        try:
            if job['operation'] == 'locate':
                if self.query_cooldown > time.time():
                    raise PoolError('Google 请求限流，查询等待冷却结束后重试', True)
                data = await self.location_pool.query(job['id'], job['device_id'])
            else:
                cmd = [sys.executable, str(ROOT/'worker.py'), job['operation']]
                if job['device_id']: cmd.append(job['device_id'])
                self.process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, cwd=ROOT)
                try:
                    out, _ = await asyncio.wait_for(self.process.communicate(), timeout=180)
                except TimeoutError:
                    self.process.kill()
                    await self.process.communicate()
                    raise TimeoutError('任务超时；请检查 Google 出口。注册任务请先同步列表确认结果。')
                result = json.loads(out)
                if not result['ok']:
                    raise RuntimeError(result['error'])
                data = result['data']
            if job['operation'] == 'locate':
                d = self.device(job['device_id'])
                points = data['points']
                history, added = merge_history(self.store.get('history:'+d['id'], []), points)
                self.store.put('history:'+d['id'], history)
                await asyncio.to_thread(central_persist, self, SOURCE, d, points, history)
                d = self.device(job['device_id'])  # Preserve pauses made while the request was in flight.
                d.update(last_fetch=time.time(), last_count=len(points), new_count=added, error=None,
                         empty_count=d.get('empty_count', 0)+int(not points), failures=0)
                d['next_run'] = time.time()+polling_seconds()
                self.store.put('device:'+d['id'], d)
                data = {'returned_count': len(points), 'new_count': added}
            job.update(state='success', result=data)
        except asyncio.CancelledError:
            job.update(state='interrupted', error='服务停止中断查询，稍后自动重试')
            raise
        except Exception as e:
            error = str(e) if isinstance(e, (RuntimeError, TimeoutError)) else f'任务失败（{type(e).__name__}）'
            job.update(state='failed', error=error)
            if isinstance(e, PoolError) and e.rate_limited:
                self.query_cooldown = max(self.query_cooldown, time.time()+900)
                self.store.put('query_cooldown', self.query_cooldown)
            if job['operation'] == 'locate':
                d = self.device(job['device_id'])
                failures = d.get('failures', 0)+1
                d.update(error=error, failures=failures, next_run=max(self.query_cooldown, time.time()+min(3600, polling_seconds()*2**min(failures, 5))))
                self.store.put('device:'+d['id'], d)
            elif job['operation'] == 'renew':
                renewal = self.store.get('renewal', {})
                failures = renewal.get('failures', 0)+1
                delay = min(3600, 300*2**min(failures-1, 4))
                self.store.put('renewal', {**renewal, 'error': error, 'failures': failures,
                                          'retry_after': time.time()+delay})
            elif job['operation'] == 'delete':
                d = self.store.get('device:'+job['device_id'])
                if d:
                    d.update(enabled=False, delete_state='failed', delete_error=error)
                    self.store.put('device:'+d['id'], d)
        finally:
            if job['operation'] != 'locate' and self.process and self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
            job['finished_at'] = time.time()
            self.store.put('job:'+job['id'], job)
            self.pending.discard((job['operation'], job['device_id']))
            self.running_jobs.pop(job['id'], None)
            self.running = next(iter(self.running_jobs), None)
            if job['operation'] != 'locate': self.process = None
            self.queue.task_done()

    async def scheduler(self):
        while True:
            sync_google(self)
            if self.ready() and not self.maintenance:
                devices = self.store.all('device:')
                renewal = self.store.get('renewal', {})
                exclusions = excluded_eids(self.store)
                if (any(d['registration'] == 'registered' and not d.get('delete_state') and not is_excluded(d, exclusions) for d in devices)
                        and time.time() > (renewal.get('retry_after', 0) if renewal.get('error')
                                           else renewal.get('last_success', 0)+86400)
                        and ('renew', None) not in self.pending and not self.queue.full()):
                    self.enqueue('renew')
                for d in sorted(devices, key=lambda d: d.get('next_run', 0)):
                    if self.query_cooldown <= time.time() and d.get('enabled') and not d.get('delete_state') and d['registration'] == 'registered' and d.get('next_run', 0) <= time.time():
                        if ('locate', d['id']) not in self.pending and not self.queue.full():
                            self.enqueue('locate', d['id'], automatic=True)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    app.state.service = Service()
    await asyncio.to_thread(central_backfill, app.state.service, SOURCE)
    tasks = [asyncio.create_task(app.state.service.worker()), asyncio.create_task(app.state.service.scheduler())]
    try:
        yield
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
install_polling(app)
install_unified_tracks(app, SOURCE)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def guard(request, next):
    if request.method not in ('GET', 'HEAD') and request.headers.get('origin') not in (None, 'http://127.0.0.1:8769', 'http://localhost:8769') and request.headers.get('origin') not in PUBLIC_ORIGINS:
        return JSONResponse({'detail': '不允许跨站操作'}, status_code=403)
    if int(request.headers.get('content-length', 0)) > 2_000_000:
        return JSONResponse({'detail': '请求过大'}, status_code=413)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('x-local-token', ''), request.app.state.service.token):
        return JSONResponse({'detail': '请刷新本地页面后操作'}, status_code=403)
    response = await next(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
                             'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://a.tile.openstreetmap.fr; frame-src {UNIFIED_MAP_ORIGIN}; base-uri 'none'; frame-ancestors 'none'"})
    return response


@app.exception_handler(RequestValidationError)
async def validation(request, error):
    return JSONResponse({'detail': '输入格式错误，请检查名称、时间间隔或授权文件'}, status_code=422)


@app.exception_handler(Exception)
async def failure(request, error):
    return JSONResponse({'detail': f'操作未完成（{type(error).__name__}）'}, status_code=500)


@app.get('/', response_class=HTMLResponse)
async def index(request: Request):
    return (ROOT/'static/index.html').read_text(encoding='utf-8').replace('__TOKEN__', request.app.state.service.token).replace('__UNIFIED_MAP_ORIGIN__', UNIFIED_MAP_ORIGIN)


@app.get('/static/{name}')
async def static(name: str):
    if name not in ('app.js', 'style.css', 'unified-track.js', 'unified-track.css', 'database-tools.js', 'polling-settings.js'): raise HTTPException(404)
    return FileResponse(ROOT/'static'/name)


@app.get('/api/status')
def status(request: Request):
    s = request.app.state.service
    exclusions = excluded_eids(s.store)
    jobs = s.store.recent_jobs()
    location_jobs = s.store.recent_jobs(locations=True)
    stats = s.store.history_stats()
    return {'account':s.store.get('auth:username'),'authorized': s.ready(), 'last_sync': s.store.get('last_sync'), 'running_job': s.running,
            'running_jobs': list(getattr(s, 'running_jobs', {})), 'query_concurrency': getattr(s, 'query_concurrency', 10),
            'query_cooldown_until': getattr(s, 'query_cooldown', 0),
            'devices': [public_device(d, s.store, exclusions, stats) for d in s.store.all('device:')], 'jobs': jobs[:30], 'location_jobs': location_jobs,
            'renewal': s.store.get('renewal', {}), 'cloud_devices': s.store.get('cloud_devices', []),
            'network_mode': os.environ.get('GOOGLEFINDMY_NETWORK_MODE', 'legacy'),
            'network_via_pc': bool(os.environ.get('GOOGLEFINDMY_PROXY')) and os.environ.get('GOOGLEFINDMY_NETWORK_MODE') != 'server_proxy'}


class NewDevice(BaseModel):
    sn: str | None = Field(default=None, pattern=r'^[1-9][0-9]{11}$')
    pubkey: str | None = Field(default=None, pattern=r'^[0-9A-Fa-f]{56}$')
    name: str = Field(min_length=1, max_length=80)
    cid: str | None = Field(default=None, pattern=r'^[0-9A-Fa-f]{8}$')


@app.post('/api/devices')
async def create(body: NewDevice, request: Request):
    if not body.name.strip(): raise HTTPException(422, '请输入设备名')
    s = request.app.state.service
    existing = s.store.all('device:')
    if body.sn:
        match = next((d for d in existing if d.get('sn')==body.sn), None)
        if match:
            if body.pubkey and match.get('pubkey','').upper()!=body.pubkey.upper():raise HTTPException(409,'SN 已存在且公钥不一致')
            return public_device(match,s.store)
    if len(existing)>=100:raise HTTPException(409,'当前账号已达到 100 台设备上限')
    device = new_device(body.name.strip())
    if body.sn:device['sn']=body.sn
    if body.pubkey:device['pubkey']=validate_pubkey(body.pubkey)
    device['interval_seconds']=polling_seconds()
    if body.cid is not None:
        device['cid'] = body.cid.upper()
    try:d = s.store.save_device_with_sn(device)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None
    return public_device(d, s.store)


@app.get('/api/devices/{id}/burn-command')
async def at_command(id: str, request: Request, cid: str | None = None):
    d = request.app.state.service.device(id)
    if d.get('cid') and cid and cid.upper() != d['cid']:
        raise HTTPException(422, 'CID 与此设备保存的固定值不一致')
    try:
        return {'command': burn_command(d, d.get('cid') or cid or DEFAULT_CID), 'sn': d['sn']}
    except ValueError as e:
        raise HTTPException(422, str(e))


class BurnConfig(BaseModel):
    cid: str = Field(default=DEFAULT_CID, pattern=r'^[0-9A-Fa-f]{8}$')
    pubkey: str = Field(pattern=r'^[0-9A-Fa-f]{56}$')


@app.get('/api/devices/{id}/burn-config')
async def get_burn_config(id: str, request: Request):
    try:
        return burn_config(request.app.state.service.device(id))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post('/api/devices/{id}/burn-config')
async def save_burn_config(id: str, body: BurnConfig, request: Request):
    s = request.app.state.service
    if s.running:
        raise HTTPException(409, '当前任务结束后再保存烧录配置')
    d = s.device(id)
    if d.get('delete_state'):
        raise HTTPException(409, '此设备正在注销，暂不能修改烧录配置')
    if d.get('cid') and d['cid'].upper() != body.cid.upper():
        raise HTTPException(422, 'CID 与此设备保存的固定值不一致')
    try:
        d = {**d, 'cid': body.cid.upper(), 'pubkey': validate_pubkey(body.pubkey)}
        result = burn_config(d)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    s.store.put('device:'+id, d)
    return result


@app.get('/api/devices/{id}/firmware')
async def firmware(id: str, request: Request):
    d = request.app.state.service.device(id)
    return Response(firmware_header(d), media_type='text/plain', headers={'Content-Disposition': f'attachment; filename="google-fmdn-{id[:8]}.h"'})


@app.get('/api/devices/{id}/identity')
async def identity(id: str, request: Request):
    d = request.app.state.service.device(id)
    return {'eik_hex': d['eik_hex'], 'note': '32 字节身份密钥，仅供安全备份；固定 EID 固件无需烧录此密钥。'}


@app.get('/api/devices/{id}/history')
async def history(id: str, request: Request):
    s = request.app.state.service
    s.device(id)
    return s.geocoder.with_cached_addresses(s.store.get('history:'+id, []))


class AddressQuery(BaseModel):
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


@app.post('/api/devices/{id}/address')
async def query_address(id: str, body: AddressQuery, request: Request):
    s = request.app.state.service
    s.device(id)
    # Restrict this service to positions actually stored for the selected device.
    if not any(p.get('latitude') == body.latitude and p.get('longitude') == body.longitude
               for p in s.store.get('history:'+id, [])):
        raise HTTPException(404, '该设备没有此坐标的位置记录')
    try:
        return await s.geocoder.lookup(body.latitude, body.longitude)
    except GeocodeError as exc:
        raise HTTPException(exc.status, str(exc))


@app.get('/api/devices/{id}/export')
async def export(id: str, request: Request):
    rows = await history(id, request)
    stream = io.StringIO()
    columns = ['timestamp', 'latitude', 'longitude', 'altitude', 'accuracy_m', 'status', 'is_own_report', 'name']
    writer = csv.DictWriter(stream, columns, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        value = {**row, 'timestamp': datetime.fromtimestamp(row['timestamp'], timezone.utc).isoformat()}
        if isinstance(value.get('name'), str) and value['name'].startswith(('=', '+', '-', '@')):
            value['name'] = "'"+value['name']
        writer.writerow(value)
    return Response('\ufeff'+stream.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="google-locations.csv"'})


@app.post('/api/jobs/{operation}')
async def job(operation: str, request: Request, device_id: str | None = None):
    if operation not in ('sync', 'register', 'locate', 'renew'): raise HTTPException(404)
    if operation in ('register', 'locate') and not device_id: raise HTTPException(422, '请选择设备')
    return request.app.state.service.enqueue(operation, device_id)


class Schedule(BaseModel):
    enabled: bool
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)


class DeleteDevice(BaseModel):
    confirm_sn: str = Field(pattern=r'^[1-9][0-9]{11}$')


@app.post('/api/devices/{id}/delete')
async def delete_device(id: str, body: DeleteDevice, request: Request):
    s = request.app.state.service
    d = s.device(id)
    if body.confirm_sn != d['sn']:
        raise HTTPException(422, '确认 SN 与所选设备不一致')
    return s.enqueue('delete', id)


class DeviceRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class DeviceSerial(BaseModel):
    sn: str = Field(pattern=r'^[1-9][0-9]{11}$')


@app.post('/api/devices/{id}/sn')
async def change_device_sn(id: str, body: DeviceSerial, request: Request):
    s = request.app.state.service
    if s.running:
        raise HTTPException(409, '当前任务结束后再修改 SN')
    s.device(id)
    try:
        d = s.store.update_device_sn(id, body.sn)
    except KeyError:
        raise HTTPException(404, '设备不存在')
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return public_device(d, s.store)


@app.post('/api/devices/{id}/rename')
async def rename_device(id: str, body: DeviceRename, request: Request):
    s = request.app.state.service
    if s.running:
        raise HTTPException(409, '当前任务结束后再修改名称')
    d = s.device(id)
    if d.get('delete_state'):
        raise HTTPException(409, '此设备等待确认注销，暂不能修改名称')
    name = body.name.strip()
    if not name:
        raise HTTPException(422, '设备名称不能为空')
    d['name'] = name
    s.store.put('device:'+id, d)
    return public_device(d, s.store)


class DeviceConfiguration(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    enabled: bool | None = None


@app.post('/api/devices/{id}/configure')
async def configure_device(id: str, body: DeviceConfiguration, request: Request):
    s = request.app.state.service
    # Avoid overwriting a device while a worker updates its query/registration data.
    if s.running:
        raise HTTPException(409, '当前任务结束后再修改设置')
    d = s.device(id)
    if d.get('delete_state'):
        raise HTTPException(409, '此设备等待确认注销，暂不能修改设置')
    name = body.name.strip()
    if not name:
        raise HTTPException(422, '设备名称不能为空')
    if body.enabled is True and (not s.ready() or d['registration'] != 'registered'):
        raise HTTPException(409, '请先完成授权和设备注册')
    interval = polling_seconds()
    interval_changed = interval != d['interval_seconds']
    d.update(name=name, interval_seconds=polling_seconds())
    if body.enabled is not None:
        d.update(enabled=body.enabled, next_run=time.time())
    elif interval_changed:
        d['next_run'] = time.time() + polling_seconds()
    s.store.put('device:'+id, d)
    return public_device(d, s.store)


@app.post('/api/devices/{id}/schedule')
async def schedule(id: str, body: Schedule, request: Request):
    s = request.app.state.service
    d = s.device(id)
    if d.get('delete_state'):
        raise HTTPException(409, '此设备等待确认注销，暂不能修改任务')
    if body.enabled and (not s.ready() or d['registration'] != 'registered'):
        raise HTTPException(409, '请先完成授权和设备注册')
    d.update(enabled=body.enabled, interval_seconds=polling_seconds(), next_run=time.time())
    s.store.put('device:'+id, d)
    return public_device(d, s.store)


@app.post('/api/auth/import')
async def import_auth(request: Request):
    s = request.app.state.service
    if s.running or s.pending: raise HTTPException(409, '请等当前任务完成后再导入授权')
    body = await request.json()
    if not isinstance(body, dict): raise HTTPException(422, '不是有效的授权文件')
    for k in ('username', 'aas_token', 'owner_key'):
        if not isinstance(body.get(k), str) or not body[k]: raise HTTPException(422, f'授权文件缺少 {k}')
    try:
        owner = bytes.fromhex(body['owner_key'])
        fcm = body['fcm_credentials']
        assert len(owner) in (16, 32) and isinstance(fcm, dict)
        assert fcm['gcm']['android_id'] and fcm['fcm']['registration']['token']
    except (KeyError, ValueError, AssertionError, TypeError):
        raise HTTPException(422, '授权文件中的加密密钥或 FCM 凭据不完整') from None
    previous = s.store.get('auth:username')
    if previous and previous.lower() != body['username'].lower() and s.store.all('device:'):
        raise HTTPException(409, '此服务已有另一账号的设备，不能直接替换账号')
    if s.running or s.pending: raise HTTPException(409, '请等当前任务完成后再导入授权')
    s.maintenance = True
    try:
        await s.location_pool.close()
        for name in ('username', 'aas_token', 'owner_key', 'fcm_credentials', 'shared_key'):
            if name in body: s.store.put('auth:'+name, body[name])
    finally:
        s.maintenance = False
    return {'imported': True, 'note': '已加密保存；请同步 Google 设备列表验证授权。'}


@app.post('/api/network')
async def network(request: Request):
    import httpx
    async def probe(host):
        try:
            async with httpx.AsyncClient(proxy=os.environ.get('GOOGLEFINDMY_PROXY'), timeout=8) as c:
                r = await c.get('https://'+host+'/')
            return {'host': host, 'reachable': True, 'http_status': r.status_code}
        except Exception as e:
            return {'host': host, 'reachable': False, 'error': type(e).__name__}
    return await asyncio.gather(*(probe(h) for h in ('accounts.google.com', 'android.googleapis.com', 'spot-pa.googleapis.com', 'firebaseinstallations.googleapis.com')))
