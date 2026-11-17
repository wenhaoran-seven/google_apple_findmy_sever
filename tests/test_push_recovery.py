import ast,asyncio,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
ROOT=Path(__file__).resolve().parents[1]/'services'

def classes(folder):
 tree=ast.parse((ROOT/folder/'location_pool.py').read_text(encoding='utf-8'))
 ns={'asyncio':asyncio,'time':time}
 nodes=[n for n in tree.body if isinstance(n,ast.ClassDef)]
 exec(compile(ast.Module(body=nodes,type_ignores=[]),'<pool>','exec'),ns)
 return ns['LocationPool'],ns['PoolError']

def wait_method(folder,error):
 tree=ast.parse((ROOT/folder/'concurrent_locations.py').read_text(encoding='utf-8'))
 cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='LocationSession')
 method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='wait_connected')
 ns={'time':time,'PushDisconnected':error};exec(compile(ast.Module(body=[method],type_ignores=[]),'<wait>','exec'),ns)
 return ns['wait_connected']

class RecoveryTests(unittest.TestCase):
 def test_stopped_or_finished_listener_fails_immediately(self):
  for folder in ['google-findmy','google-findmy-2','google-findmy-3']:
   _,error=classes(folder);wait=wait_method(folder,error)
   for state,tasks,started in [('STOPPED',[],False),('STOPPING',[],False),('STARTED',[SimpleNamespace(done=lambda:True)],True)]:
    pc=SimpleNamespace(run_state=SimpleNamespace(name=state),tasks=tasks,is_started=lambda:started)
    begin=time.monotonic()
    with self.assertRaises(error):wait(SimpleNamespace(receiver=SimpleNamespace(pc=pc)))
    self.assertLess(time.monotonic()-begin,.1)
   pc=SimpleNamespace(run_state=SimpleNamespace(name='STARTED'),tasks=[SimpleNamespace(done=lambda:False)],is_started=lambda:True)
   wait(SimpleNamespace(receiver=SimpleNamespace(pc=pc)))

 def test_reconnect_once_only_for_broken_push(self):
  async def run():
   for folder in ['google-findmy','google-findmy-2','google-findmy-3']:
    Pool,Error=classes(folder);p=Pool()
    p._query_once=AsyncMock(side_effect=[Error('disconnect',reset_connection=True),{'points':[]}])
    self.assertEqual(await p.query('job','device'),{'points':[]});self.assertEqual(p._query_once.await_count,2)
    for rate,reset,cooldown,expected in [(False,False,0,1),(True,False,0,1),(False,True,time.monotonic()+60,1),(False,True,0,2)]:
     p.restart_after=cooldown;p._query_once=AsyncMock(side_effect=Error('failed',rate_limited=rate,reset_connection=reset))
     with self.assertRaises(Error):await p.query('job','device')
     self.assertEqual(p._query_once.await_count,expected)
  asyncio.run(run())

if __name__=='__main__':unittest.main()
