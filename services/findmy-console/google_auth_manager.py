"""Root-owned narrow launcher for one resource-limited authorization desktop."""
import json, os, pwd, secrets, shutil, subprocess, time, logging
from pathlib import Path
from cryptography.fernet import Fernet
import app as host
import cloud_auth as auth
import control_store as store

BASE=Path('/var/lib/findmy-google-auth')
UNIT='findmy-google-auth-session.service'

def command(args):
    return subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25,check=True)

def stop():
    subprocess.run(['systemctl','stop',UNIT],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=25)

def directory(ident):
    import re
    if not re.fullmatch('[a-f0-9]{32}',ident):raise ValueError('Invalid session ID')
    return BASE/ident

def clean(ident):
    root=directory(ident)
    if root.exists():shutil.rmtree(root)

def launch(row):
    a=host.account(row['account'])
    if a['platform']!='google':raise ValueError('Not Google')
    unit={'google1':'google-findmy','google2':'google-findmy-2','google3':'google-findmy-3'}.get(a['id'],'findmy-account-'+a['id'])
    pid=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','--value'],text=True).strip()
    if not pid.isdigit() or int(pid)<=1:raise RuntimeError('Account service not running')
    env=dict(v.split('=',1) for v in Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in v)
    root=directory(row['id']);root.mkdir(mode=0o700)
    user=pwd.getpwnam('findmyauth');os.chown(root,user.pw_uid,user.pw_gid)
    proxy=env.get('GOOGLEFINDMY_PROXY')
    if not proxy:raise RuntimeError('Account proxy is not configured')
    code_root=str(Path('/proc/'+pid+'/cwd').resolve())
    if not Path(code_root,'bootstrap.py').is_file():raise ValueError('Invalid collector directory')
    config={'email':row['email'],'account':row['account'],'proxy':proxy,'code_root':code_root,
            'vnc_password':store.unpack(row['vnc_secret']),'result_key':Fernet.generate_key().decode()}
    if os.environ.get('FINDMY_AUTH_TEST_MODE')=='1':config['test_mode']=True
    (root/'config.json').write_text(json.dumps(config));os.chmod(root/'config.json',0o600);os.chown(root/'config.json',user.pw_uid,user.pw_gid)
    shutil.copy2('/opt/findmy-google-auth/chromedriver',root/'chromedriver');os.chown(root/'chromedriver',user.pw_uid,user.pw_gid);os.chmod(root/'chromedriver',0o700)
    stop()
    subprocess.run(['systemctl','reset-failed',UNIT],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    command(['systemd-run','--quiet','--collect','--unit='+UNIT,'--uid=findmyauth','--gid=findmyauth',
        '--property=RuntimeMaxSec=1200','--property=MemoryHigh=700M','--property=MemoryMax=900M',
        '--property=CPUQuota=75%','--property=CPUWeight=10','--property=IOWeight=10','--property=TasksMax=240',
        '--property=KillMode=control-group','--property=TimeoutStopSec=10','--property=PrivateTmp=yes',
        '--property=ProtectSystem=strict','--property=ProtectHome=yes','--property=NoNewPrivileges=yes',
        '--property=ReadWritePaths='+str(root),'--property=UMask=0077',
        '--setenv=FINDMY_AUTH_SESSION='+str(root),'/opt/google-findmy/.venv/bin/python','/opt/findmy-console/google_auth_runner.py'])
    if auth.get(row['id'])['state']=='queued':auth.update(row['id'],'starting')

def process(row):
    ident=row['id'];root=directory(ident)
    if row['state']=='cancel_requested' or time.time()>row['expires']:
        stop();clean(ident);auth.update(ident,'cancelled' if row['state']=='cancel_requested' else 'expired');return
    if row['state']=='queued':launch(row);return
    status_path=root/'status.json'
    data=json.loads(status_path.read_text()) if status_path.exists() else {}
    if data.get('state')=='failed':
        if (root/'failure.json').exists():logging.error('Authorization diagnostics: %s',(root/'failure.json').read_text())
        stop();clean(ident);auth.update(ident,'failed',data.get('error','授权失败'));return
    if (root/'result.enc').exists():
        # Browser and desktop are stopped before waiting for an idle collector.
        stop()
        config=json.loads((root/'config.json').read_text())
        values=json.loads(Fernet(config['result_key'].encode()).decrypt((root/'result.enc').read_bytes()))
        if values.get('username','').lower()!=row['email'].lower():raise ValueError('Account mismatch')
        if auth.get(ident)['state']=='cancel_requested':
            clean(ident);auth.update(ident,'cancelled');return
        try:
            host.relay(host.account(row['account']),'auth/import',values,'POST')
        except host.HTTPException as e:
            if e.status_code==409:
                auth.update(ident,'waiting_import','已完成 Google 验证，等待当前采集任务结束后保存');return
            raise
        clean(ident);auth.update(ident,'complete');return
    if data.get('state') in ('google_login','encryption') and data['state']!=row['state']:
        auth.update(ident,data['state'])
    result=subprocess.run(['systemctl','is-active','--quiet',UNIT]).returncode
    if result and time.time()-row['updated']>15:raise RuntimeError('Authorization browser exited')

def main():
    host.initialize();BASE.mkdir(mode=0o711,exist_ok=True);BASE.chmod(0o711)
    stop()
    with store.connect() as c:
        stale=c.execute("SELECT id FROM google_auth_sessions WHERE state NOT IN ('complete','failed','cancelled','expired','interrupted')").fetchall()
    for row in stale:clean(row['id']);auth.update(row['id'],'interrupted','授权服务重启，请重新启动授权')
    while True:
        with store.connect() as c:
            c.execute('INSERT OR REPLACE INTO google_auth_health VALUES(1,?)',(time.time(),))
            rows=c.execute("SELECT * FROM google_auth_sessions WHERE state NOT IN ('complete','failed','cancelled','expired','interrupted') ORDER BY created").fetchall()
        for row in rows:
            try:process(dict(row))
            except Exception as e:
                stop();clean(row['id']);auth.update(row['id'],'failed','云端授权未完成（'+type(e).__name__+'），请重新打开授权窗口')
        time.sleep(2)

if __name__=='__main__':
    import fcntl
    lock=open('/run/findmy-google-auth.lock','w');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    main()
