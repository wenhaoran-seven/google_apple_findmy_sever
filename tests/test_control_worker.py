import os, sys, tempfile, unittest, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/findmy-console'))
import app as host
import control_store as store
import control_worker as worker
from fastapi.testclient import TestClient

class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=patch.dict(os.environ,{'FINDMY_LOCATION_DB':str(Path(self.tmp.name)/'locations.db'),'FINDMY_CONTROL_DB':str(Path(self.tmp.name)/'control.db')})
        self.env.start();host.initialize();host.cached=None;host.cached_at=0
        self.client=TestClient(host.app)
        self.client.headers.update({'X-Local-Token':host.TOKEN,'X-Control-Async':'1'})
    def tearDown(self):self.env.stop();self.tmp.cleanup()

    def seed(self):
        a=host.account('google1')
        store.save_projection({**a,'available':True,'authorized':True,'devices':[{'id':'d','sn':'123456789012','name':'old'}]})
        return a

    def test_failed_poll_and_restart_preserve_inventory_and_names(self):
        a=self.seed();store.rename('sn:123456789012','saved')
        store.save_projection({**a,'available':False,'error':'offline','devices':[]})
        host.initialize()
        with patch.object(host,'relay',side_effect=AssertionError('network on web')):
            r=self.client.get('/api/console');self.assertEqual(r.status_code,200)
            self.assertEqual(r.json()['devices'][0]['name'],'saved')
            account=next(a for a in r.json()['accounts'] if a['id']=='google1')
            self.assertFalse(account['available']);self.assertEqual(account['error'],'offline')
            self.assertEqual(self.client.get('/view/devices').status_code,200)

    def test_cold_web_does_not_call_collectors(self):
        with patch.object(host,'relay',side_effect=AssertionError('network on web')):
            with TestClient(host.app) as client:
                r=client.get('/api/console',headers={'X-Local-Token':host.TOKEN})
                self.assertEqual(r.status_code,200)
                self.assertEqual(r.json()['devices'],[])

    def test_encrypted_commands_dedup_execution_and_recovery(self):
        with patch.object(host,'relay',side_effect=AssertionError('network on web')):
            r=self.client.post('/api/accounts/apple1/proxy/login',json={'password':'SENSITIVE-PASSWORD'})
            self.assertEqual(r.status_code,202)
            ident=r.json()['control_task']
            again=self.client.post('/api/accounts/apple1/proxy/login',json={'password':'SENSITIVE-PASSWORD'})
            self.assertEqual(ident,again.json()['control_task'])
        with store.connect() as c:
            row=c.execute('SELECT payload FROM commands').fetchone()
            self.assertNotIn('SENSITIVE',row[0])
        job=store.claim('apple1');self.assertEqual(job['id'],ident)
        store.recover();self.assertIsNone(store.claim('apple1'))
        self.assertEqual(store.read(ident)['state'],'uncertain')
        self.assertIsNone(store.unpack(next(iter(self.rows()))['payload']))
        other=store.submit('google1','status',None,'GET')
        with patch.object(host,'relay',return_value={'authorized':True}) as relay:
            worker.execute(store.claim('google1'));self.assertEqual(relay.call_count,1)
        self.assertEqual(store.read(other)['result'],{'authorized':True})

    def rows(self):
        with store.connect() as c:return c.execute('SELECT * FROM commands ORDER BY created').fetchall()

    def test_projection_stale_worker_does_not_remove_devices(self):
        self.seed()
        with store.connect() as c:c.execute('UPDATE projections SET checked=?',(time.time()-300,))
        result=host.snapshot()
        self.assertEqual(len(result['devices']),1)
        self.assertFalse(next(a for a in result['accounts'] if a['id']=='google1')['available'])

    def test_public_cannot_poll_secret_commands(self):
        ident=store.submit('google1','devices/d/identity',None,'GET')
        store.finish(ident,{'private_key':'secret'})
        client=TestClient(host.app)
        self.assertEqual(client.get('/api/control-tasks/'+ident).status_code,403)
        self.assertEqual(client.get('/view/control-tasks/'+ident).status_code,403)

    def test_display_does_not_wait_for_database(self):
        import asyncio
        host.cached={'accounts':[],'devices':[{'name':'saved'}]};host.cached_at=time.time()
        with patch.object(host,'get_snapshot',side_effect=AssertionError('display waits for DB')):
            self.assertEqual(asyncio.run(host.display_snapshot())['devices'][0]['name'],'saved')

    def test_inflight_old_snapshot_cannot_replace_new_name(self):
        import asyncio
        async def run():
            started=asyncio.Event();release=asyncio.Event()
            async def read():
                started.set();await release.wait()
                return {'accounts':[],'devices':[{'name':'old'}]}
            host.cached={'accounts':[],'devices':[{'name':'new'}]}
            with patch.object(host,'get_snapshot',side_effect=read):
                task=asyncio.create_task(host.warm_database())
                await started.wait();host.invalidate();release.set()
                await asyncio.sleep(.01)
                task.cancel();await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(host.cached['devices'][0]['name'],'new')
        asyncio.run(run())
