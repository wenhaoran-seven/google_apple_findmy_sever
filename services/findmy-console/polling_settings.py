"""One durable polling interval shared by Apple and all Google accounts."""
import hashlib,json,time
from fastapi import HTTPException
from pydantic import BaseModel,Field
try:
    from .central_locations import connect
except ImportError:
    from central_locations import connect

def settings():
    with connect() as con:
        row=con.execute("SELECT value FROM metadata WHERE key='global_polling'").fetchone()
    return json.loads(row[0]) if row else {'interval_seconds':300,'revision':0}

def seconds():return settings()['interval_seconds']

def stagger(identifier,interval):
    return int.from_bytes(hashlib.sha256(identifier.encode()).digest()[:4],'big')%interval

def save(minutes):
    if type(minutes) is not int or not 1<=minutes<=1440:raise ValueError('查询间隔须为 1–1440 整数分钟')
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row=con.execute("SELECT value FROM metadata WHERE key='global_polling'").fetchone()
        old=json.loads(row[0]) if row else {'revision':0}
        value={'interval_seconds':minutes*60,'revision':old['revision']+1,'updated_at':time.time()}
        con.execute("INSERT OR REPLACE INTO metadata VALUES('global_polling',?)",(json.dumps(value),))
    return value

def sync_google(service):
    value=settings()
    if getattr(service,'_polling_revision',None)==value['revision']:return
    interval=value['interval_seconds'];now=time.time()
    # Registration workers also write devices; update the latest rows atomically.
    with service.store.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        rows=con.execute("SELECT name,value FROM kv WHERE name LIKE 'device:%'").fetchall()
        for name,raw in rows:
            d=json.loads(service.store.cipher.decrypt(raw[:12],raw[12:],name.encode()))
            changed=d.get('interval_seconds')!=interval
            d['interval_seconds']=interval
            if changed and d.get('enabled'):
                due=now+stagger(d['id'],interval)
                d['next_run']=max(due,d.get('next_run') or 0) if d.get('failures') else due
            con.execute('UPDATE kv SET value=? WHERE name=?',(service.store._encrypted(name,d),name))
    service._polling_revision=value['revision']

class SettingsInput(BaseModel):
    interval_minutes:int=Field(ge=1,le=1440,strict=True)

def install(app):
    @app.get('/api/polling-settings')
    def read():return settings()
    @app.post('/api/polling-settings')
    def update(body:SettingsInput):return save(body.interval_minutes)
