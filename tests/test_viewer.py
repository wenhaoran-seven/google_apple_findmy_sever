import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"services"/"findmy-console"))
import unittest
from unittest.mock import patch,AsyncMock
from fastapi.testclient import TestClient
import app
class ViewerTests(unittest.TestCase):
 def test_public_data_projection_and_mutations(self):
  snapshot={'accounts':[{'email':'SECRET'}],'devices':[{'name':'test','sn':'123456789012','mac':'ABC','pubkey':'SECRET','members':[{'eik':'SECRET'}],'latest':{'timestamp':1,'latitude':22,'longitude':114,'status':2,'platform':'google','private_key':'SECRET','address':{'address':'address','secret':'SECRET'}},'battery':{'percent':50,'secret':'SECRET'}}]}
  with patch.object(app,'display_snapshot',new=AsyncMock(return_value=snapshot)):
   client=TestClient(app.app)
   r=client.get('/view/devices');self.assertEqual(r.status_code,200);self.assertNotIn('SECRET',r.text);self.assertTrue(r.json()['readonly'])
   html=client.get('/view').text;self.assertNotIn('local-token',html);self.assertNotIn('__TOKEN__',html)
   for method in ('POST','PUT','PATCH','DELETE'):
    self.assertEqual(client.request(method,'/view/devices').status_code,403)
   for path in ('/view/api/accounts','/view/../api/accounts','/api/console','/api/accounts'):
    self.assertIn(client.get(path).status_code,(401,403))
   self.assertEqual(client.get('/view/index.html').status_code,403)
 def test_public_track_is_readonly_and_sanitized(self):
  value={'sn':'123456789012','total':1,'has_more':False,'next_cursor':None,'earliest':1,'latest':1,'reports':[{'timestamp':1,'latitude':22,'longitude':114,'private_key':'SECRET','unknown_11':8,'reverse_address':{'found':True,'address':'test','provider':'baidu','secret':'SECRET'}}]}
  with patch.object(app.db,'read_track',return_value=value) as read:
   client=TestClient(app.app);r=client.get('/view/track?sn=123456789012&limit=10')
   self.assertEqual(r.status_code,200);self.assertNotIn('SECRET',r.text);self.assertNotIn('unknown_11',r.text)
   self.assertEqual(read.call_args.kwargs['limit'],10)
   self.assertEqual(client.post('/view/track?sn=123456789012').status_code,403)
   self.assertEqual(client.get('/view/track?sn=invalid').status_code,422)
if __name__=='__main__':unittest.main()
