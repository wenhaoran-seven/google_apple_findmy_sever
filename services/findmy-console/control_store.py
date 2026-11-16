"""Durable web projection and encrypted commands, isolated from location writes."""
import json, os, sqlite3, time, uuid
from pathlib import Path
from contextlib import contextmanager
from cryptography.fernet import Fernet
from fastapi import HTTPException
import central_locations as locations

def path():
    return Path(os.environ.get('FINDMY_CONTROL_DB', str(locations.database_path().parent/'control.sqlite3')))

@contextmanager
def connect():
    c=sqlite3.connect(path(),timeout=3)
    c.row_factory=sqlite3.Row
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback();raise
    finally:c.close()

def cipher():
    return Fernet(path().with_suffix('.key').read_bytes())

def pack(value):return cipher().encrypt(json.dumps(value,ensure_ascii=False).encode()).decode()
def unpack(value):return json.loads(cipher().decrypt(value.encode()))

def initialize():
    path().parent.mkdir(parents=True,exist_ok=True)
    key=path().with_suffix('.key')
    try:
        fd=os.open(key,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:pass
    else:
        with os.fdopen(fd,'wb') as f:f.write(Fernet.generate_key())
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS projections(account TEXT PRIMARY KEY,payload TEXT,updated REAL,checked REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS names(device_key TEXT PRIMARY KEY,name TEXT,revision TEXT,updated REAL,error TEXT);
        CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,account TEXT,path TEXT,method TEXT,payload TEXT,state TEXT,result TEXT,code INTEGER,created REAL,updated REAL);
        CREATE INDEX IF NOT EXISTS command_pending ON commands(state,created);
        ''')

def save_projection(a):
    now=time.time()
    with connect() as c:
        if a.get('available'):
            c.execute('INSERT OR REPLACE INTO projections VALUES(?,?,?,?,NULL)',(a['id'],pack(a),now,now))
        else:
            c.execute('INSERT INTO projections VALUES(?,?,0,?,?) ON CONFLICT(account) DO UPDATE SET checked=excluded.checked,error=excluded.error',
                      (a['id'],pack(a),now,a.get('error') or '账号环境尚未就绪'))

def projections(accounts):
    with connect() as c:rows={r['account']:dict(r) for r in c.execute('SELECT * FROM projections')}
    out=[]
    for a in accounts:
        r=rows.get(a['id'])
        if not r:out.append({**a,'devices':[],'available':False,'error':'等待后台同步账号数据'});continue
        data=unpack(r['payload']);data.update(a)
        data.update(snapshot_at=r['updated'],checked_at=r['checked'])
        data['available']=not bool(r['error']) and time.time()-r['checked']<180 and a['state']=='ready'
        data['error']=r['error'] or (None if data['available'] else '后台同步暂未更新，显示已保存数据')
        out.append(data)
    return out

def names():
    with connect() as c:return {r['device_key']:dict(r) for r in c.execute('SELECT * FROM names')}

def rename(key,name):
    with connect() as c:c.execute('INSERT OR REPLACE INTO names VALUES(?,?,?,?,NULL)',(key,name,str(uuid.uuid4()),time.time()))

def submit(account,path,body,method):
    ident=str(uuid.uuid4());now=time.time()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        # Identical in-flight requests share a task; never replay uncertain writes.
        for r in c.execute("SELECT id,payload FROM commands WHERE account=? AND path=? AND method=? AND state IN ('queued','running')",(account,path,method)):
            if unpack(r['payload'])==body:return r['id']
        c.execute('INSERT INTO commands VALUES(?,?,?,?,?,\'queued\',NULL,NULL,?,?)',(ident,account,path,method,pack(body),now,now))
    return ident

def read(ident):
    with connect() as c:r=c.execute('SELECT * FROM commands WHERE id=?',(ident,)).fetchone()
    if not r:raise HTTPException(404,'后台任务不存在或已过期')
    return {'task_id':r['id'],'state':r['state'],'code':r['code'],'result':unpack(r['result']) if r['result'] else None}

def recover():
    with connect() as c:
        c.execute("UPDATE commands SET state='uncertain',payload=?,result=?,code=409,updated=? WHERE state='running'",(pack(None),pack({'detail':'后台进程中断，操作结果未确认，请核对后再操作'}),time.time()))

def claim(account):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        r=c.execute("SELECT * FROM commands WHERE account=? AND state='queued' ORDER BY created LIMIT 1",(account,)).fetchone()
        if r:c.execute("UPDATE commands SET state='running',updated=? WHERE id=?",(time.time(),r['id']))
        return dict(r) if r else None

def finish(ident,result,code=200):
    with connect() as c:c.execute('UPDATE commands SET state=?,result=?,payload=?,code=?,updated=? WHERE id=?',('done' if code<400 else 'failed',pack(result),pack(None),code,time.time(),ident))

def cleanup():
    with connect() as c:
        c.execute("UPDATE commands SET state='failed',payload=?,result=?,code=503,updated=? WHERE state='queued' AND created<?",(pack(None),pack({'detail':'后台任务等待超时，未执行，请核对后台服务'}),time.time(),time.time()-900))
        c.execute("DELETE FROM commands WHERE state NOT IN ('queued','running') AND updated<?",(time.time()-3600,))
