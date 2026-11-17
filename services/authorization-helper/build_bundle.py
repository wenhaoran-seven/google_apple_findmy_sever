"""Rebuild a credential-free helper ZIP from the upstream source template."""
import argparse,json,zipfile
from pathlib import Path

def build(source,output,ca,base=None,account=None):
    root=Path(__file__).parent
    additions={p.name:p.read_bytes() for p in root.iterdir() if p.is_file() and p.name!='build_bundle.py'}
    additions['fcm_compat.py']=(root.parent/'google-findmy/fcm_compat.py').read_bytes()
    additions['company-root-ca.crt']=Path(ca).read_bytes()
    if base and account:additions['target.json']=json.dumps({'base':base,'account_id':account}).encode()
    with zipfile.ZipFile(source) as src,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            if name in additions or name=='target.json':continue
            if any(x in Path(name).parts for x in ('auth-local','exports','logs','.venv','__pycache__')):
                raise ValueError('Template contains runtime data')
            dst.writestr(name,src.read(name))
        for name,data in additions.items():dst.writestr(name,data)
    with zipfile.ZipFile(output) as archive:
        for name in ('bootstrap.py','fcm_compat.py','store.py','authorize.py','transfer.py','check_dependencies.py'):
            compile(archive.read(name),name,'exec')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('output');p.add_argument('--ca',required=True);p.add_argument('--base');p.add_argument('--account');a=p.parse_args()
    build(a.source,a.output,a.ca,a.base,a.account)
