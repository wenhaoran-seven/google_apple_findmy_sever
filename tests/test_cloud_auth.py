import os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'services/findmy-console'))
import app as host
import cloud_auth as auth
from fastapi.testclient import TestClient
class CloudAuthorizationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'FINDMY_LOCATION_DB':str(Path(self.tmp.name)/'locations.sqlite3'),'FINDMY_CONTROL_DB':str(Path(self.tmp.name)/'control.sqlite3')});self.env.start();host.initialize()
  self.client=TestClient(host.app);self.client.headers['X-Local-Token']=host.TOKEN
  with auth.store.connect() as c:c.execute('INSERT OR REPLACE INTO google_auth_health VALUES(1,?)',(time.time(),))
 def tearDown(self):self.env.stop();self.tmp.cleanup()
 def start(self):return self.client.post('/api/accounts/google1/cloud-authorization',json={'email':'test@example.com'})
 def test_no_token_no_start(self):
  self.client.headers.pop('X-Local-Token');self.assertEqual(self.start().status_code,403)
 def test_singleton_and_cancel(self):
  r=self.start();self.assertEqual(r.status_code,200);ident=r.json()['id'];self.assertEqual(self.start().status_code,409)
  self.client.post('/api/accounts/google1/cloud-authorization/'+ident+'/cancel');self.assertEqual(auth.get(ident)['state'],'cancel_requested');self.assertEqual(self.start().status_code,409)
 def test_cookie_scope_and_expiry(self):
  r=self.start();ident=r.json()['id'];url='/auth-browser/'+ident+'/connection'
  self.assertEqual(self.client.get(url).status_code,403)
  cookie=r.headers['set-cookie'].split(';')[0]
  response=self.client.get(url,headers={'Cookie':cookie})
  self.assertEqual(response.status_code,200)
  self.assertIn("img-src 'self' data:",response.headers['content-security-policy'])
  self.assertNotIn("img-src 'self' data:",self.client.get('/view').headers['content-security-policy'])
  self.assertEqual(self.client.get('/auth-browser/wrong/connection',headers={'Cookie':cookie}).status_code,403)
  self.assertEqual(self.client.post('/auth-browser/'+ident+'/cancel',headers={'Cookie':cookie,'Origin':'https://evil.example','X-Auth-Action':'cancel'}).status_code,403)
  with auth.store.connect() as c:c.execute('UPDATE google_auth_sessions SET expires=? WHERE id=?',(time.time()-1,ident))
  self.assertEqual(self.client.get(url,headers={'Cookie':cookie}).status_code,403)
 def test_existing_device_account_cannot_change(self):
  host.control.save_projection({**host.account('google1'),'email':'original@example.com','devices':[{'id':'device'}],'available':True})
  self.assertEqual(self.start().status_code,409)
 def test_service_down(self):
  with auth.store.connect() as c:c.execute('DELETE FROM google_auth_health')
  self.assertEqual(self.start().status_code,503)
