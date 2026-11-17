"""Unprivileged, single-session virtual desktop and Google authorization flow."""
import asyncio, base64, contextlib, json, logging, os, socket, subprocess, sys, threading, time
from pathlib import Path
from urllib.parse import urlsplit, unquote, urlencode
from cryptography.fernet import Fernet

ROOT=Path(os.environ['FINDMY_AUTH_SESSION'])
CONFIG=json.loads((ROOT/'config.json').read_text())

def status(state,error=None):
    p=ROOT/'status.tmp';p.write_text(json.dumps({'state':state,'error':error,'updated':time.time()}));p.replace(ROOT/'status.json')

async def relay(reader,writer):
    remote=None
    try:
        header=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),10)
        method,target,_=header.split(b'\r\n')[0].decode().split();host,port=target.rsplit(':',1)
        domains=('google.com','googleapis.com','gstatic.com','googleusercontent.com','accounts.youtube.com')
        if method!='CONNECT' or port!='443' or not any(host==d or host.endswith('.'+d) for d in domains):raise ValueError('Blocked target')
        p=urlsplit(CONFIG['proxy'])
        upstream,remote=await asyncio.wait_for(asyncio.open_connection(p.hostname,p.port or 80),10)
        auth=''
        if p.username is not None:
            value=base64.b64encode((unquote(p.username)+':'+unquote(p.password or '')).encode()).decode()
            auth='Proxy-Authorization: Basic '+value+'\r\n'
        remote.write((f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n').encode());await remote.drain()
        answer=await asyncio.wait_for(upstream.readuntil(b'\r\n\r\n'),15)
        if answer.split(b'\r\n')[0].split()[1]!=b'200':raise OSError('Proxy rejected connection')
        writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n');await writer.drain()
        async def pipe(source,dest):
            while data:=await source.read(65536):dest.write(data);await dest.drain()
        tasks=[asyncio.create_task(pipe(reader,remote)),asyncio.create_task(pipe(upstream,writer))]
        try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    except Exception:pass
    finally:
        writer.close()
        if remote:remote.close()

def proxy_thread():
    async def main():
        server=await asyncio.start_server(relay,'127.0.0.1',18981,limit=16384)
        async with server:await server.serve_forever()
    asyncio.run(main())

def configure_shared_key_flow(flow):
    """Replace only the CLI confirmation wrapper, retaining upstream key caching."""
    import KeyBackup.shared_key_flow as shared
    import KeyBackup.shared_key_retrieval as retrieval
    shared.request_shared_key_flow=flow
    retrieval._retrieve_shared_key=flow


def main():
    logging.disable(logging.CRITICAL)
    os.environ.update(DISPLAY=':91',HOME=str(ROOT),GOOGLEFINDMY_PROXY=CONFIG['proxy'],HTTP_PROXY=CONFIG['proxy'],HTTPS_PROXY=CONFIG['proxy'],NO_PROXY='127.0.0.1,localhost')
    processes=[];browser=None
    def launch(args):
        p=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);processes.append(p);return p
    try:
        status('starting')
        launch(['Xvfb',':91','-screen','0','1280x800x24','-nolisten','tcp','-ac'])
        time.sleep(1)
        launch(['openbox'])
        subprocess.run(['x11vnc','-storepasswd',CONFIG['vnc_password'],str(ROOT/'vnc.pass')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        launch(['x11vnc','-display',':91','-localhost','-rfbport','18979','-forever','-shared','-rfbauth',str(ROOT/'vnc.pass'),'-quiet'])
        threading.Thread(target=proxy_thread,daemon=True).start()
        sys.path.insert(0,CONFIG['code_root'])
        import bootstrap
        from store import Store
        store=Store(ROOT/'credentials')
        bootstrap.configure_cache(store);bootstrap.configure_network();bootstrap.fix_fcm_loop()
        store.put('auth:username',CONFIG['email'])
        import undetected_chromedriver as uc
        options=uc.ChromeOptions()
        options.page_load_strategy="eager"
        for arg in ['--window-size=1280,800','--window-position=0,0','--disable-dev-shm-usage','--no-first-run','--disable-background-networking','--disable-quic','--proxy-server=http://127.0.0.1:18981']:
            options.add_argument(arg)
        options.add_experimental_option('prefs',{'credentials_enable_service':False,'profile.password_manager_enabled':False})
        browser=uc.Chrome(options=options,user_data_dir=str(ROOT/'profile'),browser_executable_path='/usr/bin/google-chrome',driver_executable_path=str(ROOT/'chromedriver'),use_subprocess=True,no_sandbox=False)
        browser.set_page_load_timeout(45)
        if CONFIG.get('test_mode'):
            # Deployment smoke test: no login and no credential replacement.
            browser.get('https://accounts.google.com/');status('google_login')
            time.sleep(90);return
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as ec
        import Auth.auth_flow as auth_flow
        def oauth():
            status('google_login')
            browser.get('https://accounts.google.com/EmbeddedSetup?'+urlencode({'Email':CONFIG['email']}))
            WebDriverWait(browser,600).until(lambda d:d.get_cookie('oauth_token') is not None)
            return browser.get_cookie('oauth_token')['value']
        auth_flow.request_oauth_account_token_flow=oauth
        import KeyBackup.shared_key_flow as shared
        def encryption():
            from KeyBackup.shared_key_request import get_security_domain_request_url
            from KeyBackup.response_parser import get_fmdn_shared_key
            status('encryption')
            # EmbeddedSetup's oauth_token is not a Google website login session.
            browser.get('https://accounts.google.com/ServiceLogin?'+urlencode({'Email':CONFIG['email'],'continue':'https://myaccount.google.com/'}))
            WebDriverWait(browser,600).until(lambda d:urlsplit(d.current_url).hostname=='myaccount.google.com')
            script="window.mm={setVaultSharedKeys:function(s,k){window.__findmyVaultKeys=k;},closeView:function(){window.__findmyVaultClosed=true;}};"
            browser.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument',{'source':script})
            browser.get(get_security_domain_request_url().strip())
            browser.execute_script(script)
            until=time.monotonic()+600
            while time.monotonic()<until:
                keys=browser.execute_script('return window.__findmyVaultKeys || null')
                if keys:return get_fmdn_shared_key(keys).hex()
                if browser.execute_script('return window.__findmyVaultClosed === true'):raise RuntimeError('Vault closed')
                time.sleep(.5)
            raise TimeoutError('Vault authorization timed out')
        configure_shared_key_flow(encryption)
        from Auth.aas_token_retrieval import get_aas_token
        from SpotApi.GetEidInfoForE2eeDevices.get_owner_key import get_owner_key
        get_aas_token()
        actual=store.get('auth:username')
        if actual.lower()!=CONFIG['email'].lower():raise ValueError('Account mismatch')
        get_owner_key()
        values={name:store.get('auth:'+name) for name in ('username','aas_token','owner_key','shared_key','fcm_credentials')}
        if not all(values[k] for k in ('username','aas_token','owner_key','fcm_credentials')):raise ValueError('Incomplete credentials')
        (ROOT/'result.enc').write_bytes(Fernet(CONFIG['result_key'].encode()).encrypt(json.dumps(values).encode()))
        status('importing')
    except BaseException as e:
        # Never persist exception bodies, Google tokens, screenshots or passwords.
        import traceback
        (ROOT/'failure.json').write_text(json.dumps({'type':type(e).__name__,'frames':[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name} for f in traceback.extract_tb(e.__traceback__)[-5:]]}))
        status('failed','授权未完成（'+type(e).__name__+'），请检查 Google 页面提示后重新启动授权')
    finally:
        if browser:
            with contextlib.suppress(Exception):browser.quit()
        for p in reversed(processes):
            with contextlib.suppress(Exception):p.terminate()
        for p in processes:
            with contextlib.suppress(Exception):p.wait(timeout=3)

if __name__=='__main__':
    with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):main()
