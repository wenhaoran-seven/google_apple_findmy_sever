import os,sys,tempfile,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/findmy-console'))
import control_store as store
from fastapi import HTTPException

class ImportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'FINDMY_CONTROL_DB':str(Path(self.tmp.name)/'control.sqlite3')});self.env.start();store.initialize()
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def test_lost_reply_reuses_completed_task_after_cleanup(self):
  key=str(uuid.uuid4());values={'test':'fake-credential'}
  first=store.submit_auth_import('google5',values,key);store.finish(first,{'imported':True})
  with patch.object(store.time,'time',return_value=time.time()+7200):store.cleanup()
  self.assertEqual(first,store.submit_auth_import('google5',values,key))
  self.assertEqual(store.read(first)['result'],{'imported':True})
  with store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM commands').fetchone()[0],1)
 def test_reject_key_reused_with_other_account_or_payload(self):
  key=str(uuid.uuid4());store.submit_auth_import('google5',{'test':1},key)
  for account,body in [('google4',{'test':1}),('google5',{'test':2})]:
   with self.assertRaises(HTTPException):store.submit_auth_import(account,body,key)
 def test_reject_invalid_id(self):
  with self.assertRaises(HTTPException):store.submit_auth_import('google5',{},'bad')
