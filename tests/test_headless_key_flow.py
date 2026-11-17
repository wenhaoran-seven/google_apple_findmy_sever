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
