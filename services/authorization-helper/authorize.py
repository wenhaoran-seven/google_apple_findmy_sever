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
    directory = ROOT/'logs'
    directory.mkdir(exist_ok=True)
    path = directory/'authorization-status.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'stage': stage, 'updated_at': time.time(), **fields}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def main():
    status('starting')
    print('Google Find Hub 本机授权。密码、验证码、安卓锁屏 PIN 只在 Google 页面输入。')
    print('请先在安卓手机开启离线查找，并使用本账号独立的授权目录。')
    from transfer import connect, upload, export_credentials
    export_only = '--export-only' in sys.argv
    connection = None if export_only else connect()
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
    if resume and existing:
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
        if export_only:
            path = export_credentials(values)
            print('授权文件已导出：'+str(path))
            print('文件包含账号访问凭据和解密密钥，请勿发聊天、邮件或提交 Git。')
        else:
            try:
                upload(values, connection)
            except Exception:
                from transfer import UploadPending
                raise UploadPending() from None
            print('授权已保存到目标服务器账号。请回网页同步 Google 设备验证。')
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
        from transfer import UploadPending
        if isinstance(e, UploadPending):
            status('upload_pending')
            print('Google 授权已完成，本机凭据已保存；上传尚未确认。请恢复网络后运行 Resume-Upload.cmd，无需重新登录 Google。')
        else:
            print(f'授权未完成（{type(e).__name__}）。请检查提示与网络；如已有完整本机授权，可运行 Resume-Upload.cmd。')
        sys.exit(1)
