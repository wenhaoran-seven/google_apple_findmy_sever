import importlib.util,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('transfer',Path(__file__).resolve().parents[1]/'services/authorization-helper/transfer.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def sample():return {'username':'test@example.com','aas_token':'test-only','owner_key':'01'*32,'fcm_credentials':{'gcm':{'android_id':1},'fcm':{'registration':{'token':'test-only'}}}}
class Response:
 def __init__(self,v):self.v=v
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self):return json.dumps(self.v).encode()
class TransferTests(unittest.TestCase):
 def test_export_is_import_compatible(self):
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)):
   p=m.export_credentials(sample());v=json.loads(p.read_text());m.validate(v);self.assertEqual(v,sample())
 def test_reject_incomplete_credentials(self):
  v=sample();v.pop('fcm_credentials')
  with self.assertRaises(ValueError):m.validate(v)
 def test_upload_submits_once_and_waits_for_success(self):
  class Opener:
   requests=[]
   def open(self,r,timeout):
    self.requests.append(r)
    return Response([{'control_task':'test-task'},{'state':'running'},{'state':'success','code':200,'result':{'imported':True}}][len(self.requests)-1])
  op=Opener()
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),patch.object(m.time,'sleep'):
   m.upload(sample(),(op,'https://example.com','google5','test-only'))
  self.assertEqual(sum(r.get_method()=='POST' for r in op.requests),1)
  self.assertEqual(len(op.requests),3)
 def test_failed_task_not_reported_as_success(self):
  class Opener:
   def open(self,r,timeout):return Response({'control_task':'test-task'} if r.get_method()=='POST' else {'state':'failed','code':409,'result':{}})
  with tempfile.TemporaryDirectory() as d,patch.object(m,'ROOT',Path(d)),self.assertRaises(RuntimeError):m.upload(sample(),(Opener(),'https://example.com','google5','test-only'))
