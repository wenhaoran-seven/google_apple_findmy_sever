"""Privileged, narrow account provisioning worker. No arbitrary command input."""
import os,re,time,subprocess,pwd,socket,logging
from pathlib import Path
import central_locations as db

def execute(args):subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)

def provision(a):
    ident=a['id'];platform=a['platform'];port=a['port']
    if platform not in ('apple','google') or not re.fullmatch(platform+r'[1-9][0-9]{0,3}',ident) or not 19000<=port<=19999:raise ValueError('Invalid managed account identity')
    user='fma_'+ident
    try:entry=pwd.getpwnam(user)
    except KeyError:
        execute(['useradd','--system','--no-create-home','--home-dir','/var/lib/findmy-accounts/'+ident,'--shell','/usr/sbin/nologin',user]);entry=pwd.getpwnam(user)
    parent=Path('/var/lib/findmy-accounts');parent.mkdir(exist_ok=True);parent.chmod(0o711)
    data=parent/ident;data.mkdir(parents=True,exist_ok=True);data.chmod(0o700);os.chown(data,entry.pw_uid,entry.pw_gid)
    base='/opt/findmy' if platform=='apple' else '/opt/google-findmy-2'
    environment=f'Environment=FINDMY_SOURCE={a["source"]}\nEnvironment=FINDMY_LOCATION_DB=/var/lib/findmy-locations/locations.sqlite3\n'
    if platform=='apple':
        environment+=f'Environment=FINDMY_DATA_DIR={data}\nEnvironment=FINDMY_PORT={port}\nEnvironment=FINDMY_ADI_BINARY=/opt/findmy-account-assets/CoreADI.x86_64\nEnvironmentFile=/etc/findmy-proxy.env\n'
    else:environment+=f'Environment=GOOGLEFINDMY_DATA={data}\nEnvironmentFile=/etc/google-findmy-proxy.env\n'
    unit=f'''[Unit]
Description=FindMy managed account {ident}
After=network-online.target
[Service]
User={user}
Group={user}
SupplementaryGroups=findmylocations
WorkingDirectory={base}
ExecStart={base}/.venv/bin/python -m uvicorn {'localfindmy.app:app' if platform=='apple' else 'app:app'} --host 127.0.0.1 --port {port} --no-access-log --log-level critical
{environment}Restart=on-failure
RestartSec=5
TimeoutStopSec=90
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths={data} /var/lib/findmy-locations
MemoryHigh=180M
MemoryMax=400M
TasksMax=100
[Install]
WantedBy=multi-user.target
'''
    unit=unit.replace('uvicorn localfindmy.app --','uvicorn localfindmy.app:app --')
    path=Path('/etc/systemd/system')/f'findmy-account-{ident}.service';path.write_text(unit)
    execute(['systemctl','daemon-reload']);execute(['systemctl','enable','--now',path.name])
    deadline=time.monotonic()+60
    while time.monotonic()<deadline:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=2):return
        except OSError:time.sleep(2)
    raise RuntimeError('Account service did not become ready')

def main():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            with db.connect() as con:rows=[dict(r) for r in con.execute("SELECT * FROM managed_accounts WHERE state='provisioning'")]
            for a in rows:
                try:provision(a);state,error='ready',None
                except Exception as e:state,error='failed','账号环境创建失败，请检查服务日志';logging.error('%s provisioning failed: %s',a['id'],type(e).__name__)
                with db.connect() as con:con.execute('UPDATE managed_accounts SET state=?,error=? WHERE id=?',(state,error,a['id']))
        except Exception as e:logging.warning('Provisioner waiting: %s',type(e).__name__)
        time.sleep(2)

if __name__=='__main__':main()
