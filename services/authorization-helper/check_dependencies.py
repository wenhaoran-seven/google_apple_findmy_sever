"""Offline dependency smoke check; never reads saved Google credentials."""
import importlib,tempfile
from pathlib import Path
import bootstrap
from store import Store

def check():
    with tempfile.TemporaryDirectory() as directory:
        bootstrap.configure_cache(Store(Path(directory)))
        bootstrap.configure_network()
        bootstrap.fix_fcm_loop()
        for name in ('chrome_driver','undetected_chromedriver','KeyBackup.shared_key_flow',
                     'KeyBackup.shared_key_retrieval','KeyBackup.shared_key_request',
                     'KeyBackup.response_parser','Auth.aas_token_retrieval',
                     'SpotApi.GetEidInfoForE2eeDevices.get_owner_key','transfer'):
            importlib.import_module(name)
    print('授权工具依赖检查通过。')

if __name__=='__main__':
    try:check()
    except ModuleNotFoundError as error:
        print('授权工具缺少模块：'+str(error.name)+'。请更新完整工具包。')
        raise SystemExit(1)
