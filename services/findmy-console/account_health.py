"""Database-only account health feed; collectors are polled by control worker."""
import asyncio,json,time
from fastapi import Request
from fastapi.responses import StreamingResponse
import control_store as store

def initialize():
    with store.connect() as c:c.execute('CREATE TABLE IF NOT EXISTS account_health(account TEXT PRIMARY KEY,payload TEXT,checked REAL)')

def save(account, status):
    recovery=status.get('auth_recovery') or {}
    transport=status.get('transport_health') or {}
    if recovery.get('state')!='reauthorization_required' and transport.get('updated',0)>recovery.get('updated',0):
        recovery=transport
    code={'healthy':'HEALTHY','retrying':'RETRYING','reauthorization_required':'REAUTH_REQUIRED'}.get(recovery.get('state'), 'PENDING_CHECK' if status.get('authorized') else 'UNAUTHORIZED')
    labels={'HEALTHY':'正常','RETRYING':'网络或服务异常，自动重试中','REAUTH_REQUIRED':'需要人工授权','PENDING_CHECK':'已保存授权，等待检查','UNAUTHORIZED':'尚未授权','UNAVAILABLE':'账号服务暂不可用'}
    if status.get('unavailable'):code='UNAVAILABLE'
    data={'id':account['id'],'label':account['label'],'code':code,'message':labels[code],
          'last_check':recovery.get('updated'), 'retry_at':recovery.get('retry_at'),
          'authorized':bool(status.get('authorized'))}
    with store.connect() as c:c.execute('INSERT OR REPLACE INTO account_health VALUES(?,?,?)',(account['id'],json.dumps(data,ensure_ascii=False),time.time()))

def read():
    with store.connect() as c:rows=c.execute('SELECT payload,checked FROM account_health ORDER BY account').fetchall()
    result=[]
    for r in rows:
        d=json.loads(r['payload'])
        if time.time()-r['checked']>30:d.update(code='STALE',message='状态连接已中断，等待恢复')
        result.append(d)
    return result

def install(app):
    @app.get('/api/account-health')
    async def health():return await asyncio.to_thread(read)

    @app.get('/api/account-health/events')
    async def events(request:Request):
        async def stream():
            previous=None;heartbeat=0
            while not await request.is_disconnected():
                data=json.dumps(await asyncio.to_thread(read),ensure_ascii=False)
                if data!=previous:
                    yield 'data: '+data+'\n\n';previous=data
                elif time.monotonic()-heartbeat>15:
                    yield ': heartbeat\n\n';heartbeat=time.monotonic()
                await asyncio.sleep(2)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
