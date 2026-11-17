import ast,sys,types,unittest
from pathlib import Path
from unittest.mock import patch,Mock

class HeadlessKeyFlowTests(unittest.TestCase):
 def test_cli_prompt_replaced_without_skipping_key_verification_or_cache(self):
  path=Path(__file__).resolve().parents[1]/'services/findmy-console/google_auth_runner.py'
  tree=ast.parse(path.read_text(encoding='utf-8'))
  helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='configure_shared_key_flow')
  ns={};exec(compile(ast.Module(body=[helper],type_ignores=[]),str(path),'exec'),ns)
  package=types.ModuleType('KeyBackup');package.__path__=[]
  flow=types.ModuleType('KeyBackup.shared_key_flow');retrieval=types.ModuleType('KeyBackup.shared_key_retrieval')
  flow.request_shared_key_flow=Mock(side_effect=AssertionError('old browser must not open'))
  exec("cache=None\ndef _retrieve_shared_key():\n input('Press Enter')\n return request_shared_key_flow()\ndef get_shared_key():\n global cache\n if cache is None:cache=_retrieve_shared_key()\n return bytes.fromhex(cache)\n",retrieval.__dict__)
  replacement=Mock(return_value='01'*32)
  with patch.dict(sys.modules,{'KeyBackup':package,'KeyBackup.shared_key_flow':flow,'KeyBackup.shared_key_retrieval':retrieval}),patch('builtins.input',side_effect=EOFError('no terminal')):
   ns['configure_shared_key_flow'](replacement)
   self.assertEqual(retrieval.get_shared_key(),b'\x01'*32)
   self.assertEqual(retrieval.get_shared_key(),b'\x01'*32)
   replacement.assert_called_once_with()
   self.assertIs(flow.request_shared_key_flow,replacement)

 def test_encryption_establishes_website_login_before_unlock(self):
  from urllib.parse import urlencode,urlsplit
  import time
  path=Path(__file__).resolve().parents[1]/'services/findmy-console/google_auth_runner.py'
  tree=ast.parse(path.read_text(encoding='utf-8'))
  encryption=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='encryption')
  request=types.ModuleType('KeyBackup.shared_key_request');request.get_security_domain_request_url=lambda:'https://accounts.google.com/encryption/unlock/android?kdi=test\n'
  parser=types.ModuleType('KeyBackup.response_parser');parser.get_fmdn_shared_key=lambda keys:b'\x01'*32
  class Browser:
   current_url='https://accounts.google.com/EmbeddedSetup'
   authenticated=False
   navigations=[]
   def get(self,url):
    if '/encryption/unlock/' in url:
     assert self.authenticated
     assert not url.endswith('\n')
    self.navigations.append(url);self.current_url=url
   def execute_cdp_cmd(self,*args):pass
   def execute_script(self,script):
    return {'mock':'vault'} if script=='return window.__findmyVaultKeys || null' else None
  browser=Browser()
  class Wait:
   def __init__(self,b,timeout):self.browser=b
   def until(self,condition):
    assert not condition(self.browser)
    self.browser.current_url='https://myaccount.google.com/'
    self.browser.authenticated=True
    assert condition(self.browser)
  ns={'browser':browser,'CONFIG':{'email':'test@example.com'},'status':Mock(),'WebDriverWait':Wait,'urlencode':urlencode,'urlsplit':urlsplit,'time':time}
  exec(compile(ast.Module(body=[encryption],type_ignores=[]),str(path),'exec'),ns)
  with patch.dict(sys.modules,{'KeyBackup.shared_key_request':request,'KeyBackup.response_parser':parser}):
   self.assertEqual(ns['encryption'](),'01'*32)
  self.assertIn('/ServiceLogin?',browser.navigations[0])
  self.assertIn('/encryption/unlock/',browser.navigations[1])
