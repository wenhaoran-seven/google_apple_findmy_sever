"""Local credential export and verified HTTPS import; never print credentials."""
import base64, getpass, json, os, re, ssl, sys, time, urllib.request, urllib.error, http.client, hashlib, uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent

class UploadPending(RuntimeError):
    pass

def request_json(opener,request,timeout=20):
    for attempt in range(4):
        try:
            with open_retry(opener,request,timeout) as response:return json.load(response)
        except (ConnectionError,TimeoutError,http.client.IncompleteRead):
            if attempt==3:raise
            time.sleep(2**attempt)

def open_retry(opener,request,timeout=20):
    for attempt in range(4):
        try:return opener.open(request,timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in (502,503,504) or attempt==3:raise
        except (urllib.error.URLError,ConnectionError,TimeoutError,http.client.RemoteDisconnected):
            if attempt==3:raise
        print('连接暂时中断，正在重试（'+str(attempt+1)+'/3）。')
        time.sleep(2**attempt)

def save_receipt(data):
    temporary=ROOT/'last-import-task.tmp'
    temporary.write_text(json.dumps(data),encoding='utf-8')
    temporary.replace(ROOT/'last-import-task.json')

def validate(values):
    for name in ('username', 'aas_token', 'owner_key'):
        if not isinstance(values.get(name), str) or not values[name]:
            raise ValueError('授权文件缺少必要字段：'+name)
    if len(bytes.fromhex(values['owner_key'])) not in (16,32):
        raise ValueError('owner_key 长度不正确')
    fcm=values.get('fcm_credentials',{})
    if not fcm.get('gcm',{}).get('android_id') or not fcm.get('fcm',{}).get('registration',{}).get('token'):
        raise ValueError('缺少完整 FCM 凭据')

def connect():
    target=json.loads((ROOT/'target.json').read_text(encoding='utf-8-sig'))
    base=target['base'].rstrip('/');account=target['account_id']
    parsed=urlsplit(base)
    if parsed.scheme!='https' or parsed.username or parsed.query or parsed.fragment or parsed.path or not re.fullmatch(r'google\d+',account):
        raise ValueError('目标必须是 HTTPS 服务器根地址和 Google 账号编号')
    print('目标服务器：'+base+'；目标账号：'+account)
    username=input('平台网页登录用户名：').strip();password=getpass.getpass('平台网页登录密码：')
    context=ssl.create_default_context()
    ca=ROOT/'company-root-ca.crt'
    if ca.exists():context.load_verify_locations(cafile=str(ca))
    manager=urllib.request.HTTPPasswordMgrWithDefaultRealm();manager.add_password(None,base,username,password)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),urllib.request.HTTPBasicAuthHandler(manager))
    with open_retry(opener,base) as r:html=r.read().decode()
    match=re.search(r'name="local-token" content="([^"]+)"',html)
    if not match:raise RuntimeError('未获取平台连接令牌，请确认地址与登录信息')
    return opener,base,account,match[1]

def upload(values, connection=None):
    validate(values)
    opener,base,account,token=connection or connect()
    digest=hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    receipt={}
    path=ROOT/'last-import-task.json'
    if path.exists():receipt=json.loads(path.read_text(encoding='utf-8'))
    if any(receipt.get(k)!=v for k,v in {'base':base,'account':account,'digest':digest}.items()):
        receipt={'base':base,'account':account,'digest':digest,'request_id':str(uuid.uuid4())}
    if not receipt.get('request_id'):receipt['request_id']=str(uuid.uuid4())
    # Persist before POST so an interrupted response can be recovered safely.
    save_receipt(receipt)
    req=urllib.request.Request(base+'/api/accounts/'+account+'/proxy/auth/import',
        data=json.dumps(values).encode(),headers={'Content-Type':'application/json','X-Local-Token':token,'X-Control-Async':'1','X-Authorization-Import-ID':receipt['request_id']})
    try:
        result=request_json(opener,req,30)
    except urllib.error.HTTPError as error:
        if error.code!=403:raise
        # A server restart during interactive Google login may rotate its token.
        with open_retry(opener,base) as r:html=r.read().decode()
        match=re.search(r'name="local-token" content="([^"]+)"',html)
        if not match:raise RuntimeError('请重新登录平台后运行补传')
        token=match[1];req.add_header('X-Local-Token',token)
        result=request_json(opener,req,30)
    task=result.get('control_task')
    if not task:
        if result.get('imported') is True:return
        raise RuntimeError('服务器未确认导入，请查看账号管理，不要重复提交')
    if not re.fullmatch(r'[A-Za-z0-9-]+',task):raise RuntimeError('任务编号无效')
    receipt['task']=task;save_receipt(receipt)
    print('已提交，正在等待服务器确认。')
    for _ in range(150):
        req=urllib.request.Request(base+'/api/control-tasks/'+task,headers={'X-Local-Token':token})
        state=request_json(opener,req,20)
        if state['state'] not in ('queued','running'):
            if state.get('code')==200 and (state.get('result') or {}).get('imported') is True:
                receipt['confirmed']=True;save_receipt(receipt);return
            raise RuntimeError('服务器未完成导入，请查看账号管理中的操作结果')
        time.sleep(2)
    raise TimeoutError('导入任务仍未确认；请查看 last-import-task.json 和网页，不要重复提交')

def export_credentials(values):
    validate(values)
    directory=ROOT/'exports';directory.mkdir(exist_ok=True)
    path=directory/('google-authorization-'+time.strftime('%Y%m%d-%H%M%S')+'.json')
    # Exclusive creation prevents overwriting an earlier authorization.
    with path.open('x',encoding='utf-8') as f:json.dump(values,f,ensure_ascii=False)
    if os.name!='nt':path.chmod(0o600)
    return path

if __name__=='__main__':
    try:
        if '--cached' in sys.argv:
            from store import Store
            store=Store(ROOT/'auth-local')
            values={k:store.get('auth:'+k) for k in ('username','aas_token','owner_key','shared_key','fcm_credentials')}
        else:
            path=input('授权 JSON 文件路径（可拖入文件）：').strip().strip('"')
            values=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        validate(values)
        print('授权邮箱：'+values['username'])
        upload(values)
        print('服务器已确认导入成功。')
    except Exception as e:
        print('未确认完成（'+type(e).__name__+'）；本机授权仍保留，请恢复网络后运行 Resume-Upload.cmd 补传缓存，无需重新登录 Google。')
        sys.exit(1)
