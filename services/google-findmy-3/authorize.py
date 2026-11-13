"""Interactive local-only Chrome login, encrypted storage and loopback upload."""
import json
import logging
import re
import sys
import tempfile
import time
import subprocess
import os
import urllib.request
from pathlib import Path
import bootstrap
from store import Store, ROOT


def status(stage, **fields):
    """Best-effort progress: a Windows reader must never abort Google login."""
    directory = ROOT/'logs'
    temporary = None
    try:
        directory.mkdir(exist_ok=True)
        path = directory/'authorization-status.json'
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=directory,
                                         prefix='authorization-status-', suffix='.tmp', delete=False) as out:
            temporary = Path(out.name)
            json.dump({'stage': stage, 'updated_at': time.time(), **fields}, out, ensure_ascii=False)
        for attempt in range(8):
            try:
                temporary.replace(path)
                return True
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.1)
    except OSError:
        print('状态日志暂时无法更新，授权将继续；请以当前窗口提示为准。', flush=True)
        return False
    finally:
        if temporary:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    status('starting')
    print('Google Find Hub 本机授权。密码、验证码、安卓锁屏 PIN 只在 Google 页面输入。')
    print('请先在安卓手机开启查找离线设备，并运行 Open-Aliyun-GoogleFindMy-3.cmd。')
    # Check tunnel before requesting credentials from the user.
    base = 'http://127.0.0.1:8771'
    html = urllib.request.urlopen(base, timeout=10).read().decode()
    token = re.search(r'name="local-token" content="([^"]+)"', html)[1]
    store = Store(ROOT/'auth-local')
    bootstrap.configure_cache(store)
    bootstrap.configure_network()
    bootstrap.fix_fcm_loop()
    logging.disable(logging.CRITICAL)
    import chrome_driver
    import undetected_chromedriver as uc
    # Upstream kills every Chrome process. Use isolated temporary profiles instead.
    profiles = []
    def driver():
        profile = tempfile.TemporaryDirectory(prefix='google-findhub-auth-')
        profiles.append(profile)
        options = uc.ChromeOptions()
        options.add_argument('--start-maximized')
        executable = chrome_driver.find_chrome()
        major = None
        if executable and os.name == 'nt':
            literal = executable.replace("'", "''")
            version = subprocess.check_output(['powershell.exe', '-NoProfile', '-Command',
                "(Get-Item -LiteralPath '"+literal+"').VersionInfo.ProductVersion"],
                text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW).strip()
            if re.match(r'^\d+\.', version):
                major = int(version.split('.')[0])
        return uc.Chrome(options=options, user_data_dir=profile.name, browser_executable_path=executable, version_main=major, use_subprocess=True)
    chrome_driver.create_driver = driver
    # Upstream's shared-key flow waits forever after a closed/locked window.
    # Keep the same Google flow, with a bounded wait and guaranteed browser cleanup.
    import KeyBackup.shared_key_flow as shared_flow
    def shared_key_flow():
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as ec
        from KeyBackup.shared_key_request import get_security_domain_request_url
        from KeyBackup.response_parser import get_fmdn_shared_key
        browser = driver()
        try:
            browser.get('https://accounts.google.com/')
            WebDriverWait(browser, 300).until(ec.url_contains('https://myaccount.google.com'))
            browser.get(get_security_domain_request_url())
            browser.execute_script("window.mm={setVaultSharedKeys:function(s,k){alert(JSON.stringify({method:'setVaultSharedKeys',vaultKeys:k}));},closeView:function(){alert(JSON.stringify({method:'closeView'}));}};")
            deadline = time.monotonic()+600
            while time.monotonic() < deadline:
                from selenium.common.exceptions import TimeoutException
                try:
                    WebDriverWait(browser, 1).until(ec.alert_is_present())
                except TimeoutException:
                    continue
                alert = browser.switch_to.alert
                result = json.loads(alert.text)
                alert.accept()
                if result.get('method') == 'setVaultSharedKeys':
                    return get_fmdn_shared_key(result['vaultKeys']).hex()
                if result.get('method') == 'closeView':
                    raise RuntimeError('Google 加密数据授权窗口已关闭')
            raise TimeoutError('Google 加密数据授权等待超时')
        finally:
            browser.quit()
    shared_flow.request_shared_key_flow = shared_key_flow
    resume = '--resume' in sys.argv
    existing = store.get('auth:username')
    if (resume or '--reauthorize' in sys.argv) and existing:
        email = existing
        print('继续使用上次输入的账号。')
    else:
        status('waiting_email')
        email = input('Google 账号邮箱：').strip()
    if '@' not in email: raise ValueError('请输入正确的邮箱')
    previous = store.get('auth:username')
    if previous and previous.lower() != email.lower():
        raise RuntimeError('本机授权缓存属于另一个账号，请使用独立授权目录。')
    store.put('auth:username', email)
    # Running the helper again must actually refresh expired credentials.
    if not resume:
        for key in ('aas_token', 'owner_key', 'shared_key'):
            store.put('auth:'+key, None)
    from Auth.aas_token_retrieval import get_aas_token
    from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
    from Auth.fcm_receiver import FcmReceiver
    try:
        status('google_login')
        get_aas_token()
        status('encryption_authorization')
        get_owner_key()
        status('uploading')
        values = {key.removeprefix('auth:'): store.get(key) for key in store.names('auth:')}
        request = urllib.request.Request(base+'/api/auth/import', data=json.dumps(values).encode(), headers={'Content-Type': 'application/json', 'X-Local-Token': token})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 200
        print('授权已通过 SSH 隧道传入云端并加密保存。请回到网页点击“同步 Google 设备”验证。')
        status('complete')
    finally:
        FcmReceiver().stop_listening()
        # Only clean up temporary browser profiles after the browser has quit.
        for profile in profiles:
            try: profile.cleanup()
            except OSError: pass


if __name__ == '__main__':
    try: main()
    except Exception as e:
        import traceback
        status('failed', error_type=type(e).__name__, frames=[{'file':Path(f.filename).name, 'function':f.name, 'line':f.lineno} for f in traceback.extract_tb(e.__traceback__)[-5:]])
        # Do not expose Google response bodies or token-bearing exception messages.
        print(f'授权未完成（{type(e).__name__}）。请确认手机已开启离线查找、Chrome 已更新，并检查网络。')
        sys.exit(1)
