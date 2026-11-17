import sys,tempfile,time,os,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/findmy-console'))
import account_health as health
import control_store as store

class HealthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'FINDMY_CONTROL_DB':str(Path(self.tmp.name)/'control.sqlite3')});self.env.start();store.initialize();health.initialize();self.a={'id':'google5','label':'Google账号5'}
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def test_manual_auth_survives_transport_success_and_secrets_are_not_exposed(self):
  health.save(self.a,{'authorized':False,'auth_recovery':{'state':'reauthorization_required','updated':10,'token':'secret'},'transport_health':{'state':'healthy','updated':20}})
  rows=health.read();self.assertEqual(rows[0]['code'],'REAUTH_REQUIRED');self.assertNotIn('secret',str(rows))
 def test_network_failure_then_recovery(self):
  health.save(self.a,{'authorized':True,'auth_recovery':{'state':'healthy','updated':10},'transport_health':{'state':'retrying','updated':20}})
  self.assertEqual(health.read()[0]['code'],'RETRYING')
  health.save(self.a,{'authorized':True,'auth_recovery':{'state':'healthy','updated':30}})
  self.assertEqual(health.read()[0]['code'],'HEALTHY')
 def test_stale_is_not_auth_revocation(self):
  health.save(self.a,{'authorized':True})
  with patch.object(health.time,'time',return_value=time.time()+40):self.assertEqual(health.read()[0]['code'],'STALE')
 def test_unconfigured_account(self):
  health.save(self.a,{'authorized':False});self.assertEqual(health.read()[0]['code'],'UNAUTHORIZED')
