import os,sys,tempfile,json,time,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/findmy-address-worker'))
import address_worker as worker
import address_provider as provider

class FallbackTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.key=Path(self.tmp.name)/'ak';self.key.write_text('test-only')
  self.env=patch.dict(os.environ,{'FINDMY_LOCATION_DB':str(Path(self.tmp.name)/'db.sqlite3'),'BAIDU_MAP_AK_FILE':str(self.key)});self.env.start()
  provider.blocked_until=0;provider.failures=0;worker.initialize()
 def tearDown(self):self.env.stop();self.tmp.cleanup();provider.blocked_until=0;provider.failures=0
 def test_quota_falls_back_and_cache_reused_after_restart(self):
  d={'id':'test','sn':'900000000001'}
  worker.db.ingest('google1','test',d,[{'timestamp':time.time(),'latitude':22,'longitude':114}])
  opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=json.dumps({'status':302}).encode()
  payload={'features':[{'properties':{'city':'test city','street':'test road'}}]}
  with patch.object(provider.urllib.request,'build_opener',return_value=opener),patch.object(worker.db,'photon_request',return_value=payload) as osm:
   result=worker.tick();self.assertTrue(result['provider_request']);osm.assert_called_once()
  self.assertGreater(provider.blocked_until,time.time());until=provider.blocked_until
  with worker.db.connect() as con:
   self.assertEqual(con.execute('SELECT COUNT(*) FROM baidu_upgrades').fetchone()[0],0)
   self.assertEqual(con.execute("SELECT status FROM address_points").fetchone()[0],'ready')
  provider.blocked_until=0;worker.initialize();self.assertEqual(provider.blocked_until,until)
  worker.db.ingest('apple','test',d,[{'timestamp':time.time(),'latitude':22.0001,'longitude':114}])
  with patch.object(provider,'reverse',side_effect=AssertionError('must reuse cache')):
   result=worker.tick();self.assertFalse(result['provider_request']);self.assertGreater(result['cache_hits'],0)
 def test_legacy_upgrade_queue_removed_without_losing_address(self):
  with worker.db.connect() as c:
   c.execute("INSERT INTO geocodes VALUES(?,?,?)",('22.0000000,114.0000000',json.dumps({'found':True,'address':'cached','provider':'Photon / OpenStreetMap'}),time.time()+86400))
   c.execute('INSERT INTO baidu_upgrades(coordinate_key) VALUES(?)',('22.0000000,114.0000000',))
  worker.initialize()
  with worker.db.connect() as c:
   self.assertEqual(c.execute('SELECT COUNT(*) FROM baidu_upgrades').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT COUNT(*) FROM geocodes').fetchone()[0],1)
if __name__=='__main__':unittest.main()
