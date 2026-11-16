import ast,asyncio,unittest
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException
ROOT=Path(__file__).resolve().parents[1]/'services'
def extract(file,name,env):
 tree=ast.parse(file.read_text(encoding='utf-8'));node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name==name)
 node.decorator_list=[]
 module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),node],type_ignores=[])
 exec(compile(ast.fix_missing_locations(module),str(file),'exec'),env);return env[name]
class Tests(unittest.TestCase):
 def test_apple_rename_does_not_wait_on_network_query_gate(self):
  class Gate:
   async def __aenter__(self):raise AssertionError('rename must not wait on network')
  data={'name':'old','sn':'123456789012','key':{}}
  writes=[];key=SimpleNamespace(name='old',to_json=lambda:{'name':'new'})
  s=SimpleNamespace(lock=Gate(),devices={'id':data},device=lambda id:(data,key),vault=SimpleNamespace(write=lambda *a:writes.append(a)))
  fn=extract(ROOT/'findmy/localfindmy/app.py','rename',{'device_summary':lambda id,d:{'id':id,'name':d['name']}})
  out=asyncio.run(fn('id',SimpleNamespace(name='new'),SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(service=s)))))
  self.assertEqual(out['name'],'new');self.assertEqual(s.devices['id']['name'],'new');self.assertEqual(len(writes),1)
 def test_google_allows_active_locations_but_blocks_exclusive_jobs(self):
  for folder in ('google-findmy','google-findmy-2','google-findmy-3'):
   data={'id':'id','name':'old'};writes=[]
   s=SimpleNamespace(running_jobs={'job':{'operation':'locate'}},device=lambda id:data,store=SimpleNamespace(put=lambda *a:writes.append(a)))
   fn=extract(ROOT/folder/'app.py','rename_device',{'HTTPException':HTTPException})
   req=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(service=s)))
   self.assertEqual(asyncio.run(fn('id',SimpleNamespace(name='new'),req))['name'],'new')
   s.running_jobs={'job':{'operation':'sync'}}
   with self.assertRaises(HTTPException):asyncio.run(fn('id',SimpleNamespace(name='other'),req))
   self.assertEqual(len(writes),1)
