"""Short-lived, cookie-bound Google remote authorization sessions."""
import asyncio, hashlib, json, re, secrets, time
from pathlib import Path
from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import control_store as store

ACTIVE=('queued','starting','google_login','encryption','importing','waiting_import')
SCHEMA='''
CREATE TABLE IF NOT EXISTS google_auth_sessions(
 id TEXT PRIMARY KEY,account TEXT,email TEXT,state TEXT,created REAL,expires REAL,
 ticket_hash TEXT,vnc_secret TEXT,error TEXT,updated REAL);
CREATE TABLE IF NOT EXISTS google_auth_health(id INTEGER PRIMARY KEY CHECK(id=1),updated REAL);
'''

def initialize():
    with store.connect() as c:c.executescript(SCHEMA)

def get(ident):
    with store.connect() as c:r=c.execute('SELECT * FROM google_auth_sessions WHERE id=?',(ident,)).fetchone()
    if not r:raise HTTPException(404,'授权会话不存在')
    return dict(r)

def public(row):
    return {k:row[k] for k in ('id','account','email','state','created','expires','error','updated')}

def update(ident,state,error=None):
    with store.connect() as c:c.execute('UPDATE google_auth_sessions SET state=?,error=?,updated=? WHERE id=?',(state,error,time.time(),ident))

def ticket(request,ident=None,active=False):
    value=request.cookies.get('findmy-google-auth','')
    if not value:raise HTTPException(403,'请从账号管理启动授权')
    digest=hashlib.sha256(value.encode()).hexdigest()
    with store.connect() as c:r=c.execute('SELECT * FROM google_auth_sessions WHERE ticket_hash=?',(digest,)).fetchone()
    if not r or (ident and r['id']!=ident) or time.time()>r['expires'] or (active and r['state'] not in ACTIVE):
        raise HTTPException(403,'授权窗口已失效，请返回账号管理重新启动')
    return dict(r)

class Start(BaseModel):
    email:str=Field(min_length=3,max_length=254,pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')

def install(app,host):
    @app.post('/api/accounts/{ident}/cloud-authorization')
    def start(ident:str,body:Start):
        a=host.account(ident)
        if a['platform']!='google':raise HTTPException(422,'仅支持 Google 账号')
        email=body.email.strip()
        projection=store.projections([a])[0]
        if projection.get('email') and projection['email'].lower()!=email.lower() and projection['devices']:
            raise HTTPException(409,'此账号已有设备，请使用原 Google 邮箱重新授权')
        now=time.time();ident_new=secrets.token_hex(16);secret=secrets.token_urlsafe(32)
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            health=c.execute('SELECT updated FROM google_auth_health WHERE id=1').fetchone()
            if not health or now-health[0]>30:raise HTTPException(503,'云端授权服务尚未就绪，请稍后重试')
            existing=c.execute("SELECT id FROM google_auth_sessions WHERE state NOT IN ('complete','failed','cancelled','expired','interrupted')").fetchone()
            if existing:raise HTTPException(409,'已有授权浏览器运行，请先完成或取消当前授权')
            c.execute('INSERT INTO google_auth_sessions VALUES(?,?,?,?,?,?,?,?,NULL,?)',
                      (ident_new,ident,email,'queued',now,now+1200,hashlib.sha256(secret.encode()).hexdigest(),store.pack(secrets.token_hex(4)),now))
        response=JSONResponse({**public(get(ident_new)),'url':'/auth-browser/'+ident_new})
        response.set_cookie('findmy-google-auth',secret,max_age=1200,secure=True,httponly=True,samesite='strict',path='/auth-browser')
        return response

    @app.get('/api/accounts/{ident}/cloud-authorization')
    def latest(ident:str):
        host.account(ident)
        with store.connect() as c:r=c.execute('SELECT * FROM google_auth_sessions WHERE account=? ORDER BY created DESC LIMIT 1',(ident,)).fetchone()
        return public(dict(r)) if r else None

    @app.post('/api/accounts/{account}/cloud-authorization/{ident}/cancel')
    def cancel(account:str,ident:str):
        r=get(ident)
        if r['account']!=account:raise HTTPException(404)
        if r['state'] in ACTIVE:update(ident,'cancel_requested')
        return public(get(ident))

    @app.get('/auth-browser/assets/{path:path}')
    def assets(path:str,request:Request):
        ticket(request)
        root=Path('/usr/share/novnc').resolve();target=(root/path).resolve()
        if not target.is_relative_to(root) or not target.is_file() or target.suffix not in ('.js','.css','.png','.svg','.woff','.woff2'):
            raise HTTPException(404)
        return FileResponse(target)

    @app.get('/auth-browser/{ident}',response_class=HTMLResponse)
    def browser(ident:str,request:Request):
        ticket(request,ident)
        return (host.ROOT/'static/cloud-auth.html').read_text(encoding='utf-8')

    @app.get('/auth-browser/{ident}/connection')
    def connection(ident:str,request:Request):
        r=ticket(request,ident)
        return {**public(r),'password':store.unpack(r['vnc_secret']) if r['state'] in ACTIVE else None}

    @app.post('/auth-browser/{ident}/cancel')
    def browser_cancel(ident:str,request:Request):
        r=ticket(request,ident)
        if request.headers.get('origin') not in host.ORIGINS or request.headers.get('x-auth-action')!='cancel':raise HTTPException(403)
        if r['state'] in ACTIVE:update(ident,'cancel_requested')
        return {'ok':True}

    @app.websocket('/auth-browser/{ident}/socket')
    async def socket(websocket:WebSocket,ident:str):
        writer=None
        try:
            if websocket.headers.get('origin') not in host.ORIGINS or websocket.headers.get('host','').split(':')[0] not in ('127.0.0.1','localhost','testserver'):
                await websocket.close(code=1008);return
            r=ticket(websocket,ident,True)
            if r['state'] not in ('google_login','encryption'):
                await websocket.close(code=1013);return
            reader,writer=await asyncio.wait_for(asyncio.open_connection('127.0.0.1',18979),3)
            await websocket.accept(subprotocol='binary' if 'binary' in websocket.scope.get('subprotocols',[]) else None)
            async def receive():
                while True:
                    data=await websocket.receive_bytes()
                    if len(data)>1024*1024:raise ValueError('Oversized VNC message')
                    writer.write(data);await writer.drain()
            async def send():
                while data:=await reader.read(65536):await websocket.send_bytes(data)
            async def expiry():
                while time.time()<r['expires'] and get(ident)['state'] in ACTIVE:await asyncio.sleep(2)
            tasks=[asyncio.create_task(f()) for f in (receive,send,expiry)]
            try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:t.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        except Exception:pass
        finally:
            if writer:writer.close()
            try:await websocket.close()
            except Exception:pass
