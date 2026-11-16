"""Unified, authenticated facade; account credentials stay in isolated collectors."""
import asyncio,hashlib,json,os,re,secrets,time,urllib.request,urllib.error,uuid,logging,socket
from pathlib import Path
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI,HTTPException,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse,FileResponse,JSONResponse,Response
from pydantic import BaseModel,Field
import central_locations as db
import dual_add
import control_store as control
import sys
from polling_settings import install as install_polling

ROOT=Path(__file__).resolve().parent
MAP=os.environ.get('FINDMY_MAP_ORIGIN','https://47.106.211.14:18447')
ORIGINS=set(json.loads(os.environ.get('FINDMY_PUBLIC_ORIGINS','["https://47.106.211.14"]')))
TOKEN=Path(os.environ['FINDMY_CONSOLE_TOKEN_FILE']).read_text().strip() if os.environ.get('FINDMY_CONSOLE_TOKEN_FILE') else secrets.token_urlsafe(32)
SCHEMA='''
CREATE TABLE IF NOT EXISTS managed_accounts(id TEXT PRIMARY KEY,platform TEXT,source TEXT UNIQUE,port INTEGER UNIQUE,label TEXT,state TEXT,error TEXT,created_at REAL);
CREATE TABLE IF NOT EXISTS burn_registry(sn TEXT PRIMARY KEY,state TEXT,session TEXT,updated_at REAL,evidence TEXT);
CREATE TABLE IF NOT EXISTS console_imports(id TEXT PRIMARY KEY,account TEXT,items TEXT,enabled INTEGER,state TEXT,results TEXT,created_at REAL,updated_at REAL);
'''

def initialize():
    db.initialize()
    dual_add.initialize()
    control.initialize()
    with db.connect() as con:
        con.executescript(SCHEMA)
        for ident,platform,source,port,label in [('apple1','apple','apple',8765,'Apple 账号1'),('google1','google','google1',8767,'Google 账号1'),('google2','google','google2',8769,'Google 账号2'),('google3','google','google3',8771,'Google 账号3')]:
            con.execute('INSERT OR IGNORE INTO managed_accounts VALUES(?,?,?,?,?,?,NULL,?)',(ident,platform,source,port,label,'ready',time.time()))
        baseline=ROOT/'burn-baseline.json'
        if baseline.is_file() and not con.execute("SELECT 1 FROM metadata WHERE key='burn_baseline_20261111'").fetchone():
            data=json.loads(baseline.read_text(encoding='utf-8'))
            for sn in data['all_sns']:
                con.execute('INSERT OR IGNORE INTO burn_registry VALUES(?,?,NULL,?,?)',(sn,'unburned' if sn in data['unburned_sns'] else 'burned',time.time(),'用户确认：仅本批48个SN未烧录'))
            con.execute("INSERT INTO metadata VALUES('burn_baseline_20261111',?)",(json.dumps({'unburned':len(data['unburned_sns']),'total':len(data['all_sns'])}),))

def accounts():
    with db.connect() as con:return [dict(r) for r in con.execute('SELECT * FROM managed_accounts ORDER BY platform,created_at,id')]

def account(ident):
    match=next((a for a in accounts() if a['id']==ident),None)
    if not match:raise HTTPException(404,'账号不存在')
    if match['state']!='ready':raise HTTPException(409,'账号环境正在准备或准备失败，请查看账号管理')
    return match

APPLE_READ=re.compile(r'(?:status|devices|schedule|2fa/methods)\Z')
APPLE_WRITE=re.compile(r'(?:login|logout|anisette|network|2fa/(?:request|submit)|keys/(?:import|import-sn)|schedule(?:/pause)?|devices/[A-Za-z0-9_-]+/(?:rename|export))\Z')
GOOGLE_READ=re.compile(r'(?:status|devices/[A-Za-z0-9_-]+/(?:burn-config|burn-command|identity))\Z')
GOOGLE_WRITE=re.compile(r'(?:auth/import|network|devices|jobs/(?:register|sync|renew)|devices/[A-Za-z0-9_-]+/(?:rename|configure|schedule|burn-config|delete))\Z')

relay_tokens={}
relay_logger=logging.getLogger('findmy.accounts')
def relay(a,path,body=None,method=None):
    method=method or ('POST' if body is not None else 'GET')
    readonly=method=='GET'
    op=urllib.request.build_opener(urllib.request.ProxyHandler({}));origin='http://127.0.0.1:'+str(a['port'])
    for attempt in range(2 if readonly else 1):
        stage='获取连接令牌';started=time.monotonic()
        try:
            entry=relay_tokens.get(origin)
            if entry and time.monotonic()-entry[1]<300:token=entry[0]
            else:
                with op.open(origin+'/',timeout=15) as r:
                    match=re.search(r'name="local-token" content="([^"]+)"',r.read().decode())
                    if not match:raise ValueError('missing local token')
                    token=match[1];relay_tokens[origin]=(token,time.monotonic())
            stage='读取账号数据' if readonly else '提交操作'
            req=urllib.request.Request(origin+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'X-Local-Token':token,'Content-Type':'application/json'})
            with op.open(req,timeout=(20 if readonly else (130 if a['platform']=='apple' else 60))) as r:return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code==403:
                relay_tokens.pop(origin,None)
                if readonly and attempt==0:continue
            try:detail=json.loads(e.read()).get('detail','操作未完成')
            except Exception:detail='操作未完成'
            raise HTTPException(e.code,detail if isinstance(detail,str) else '输入格式错误') from None
        except Exception as e:
            relay_logger.warning('account=%s method=%s path=%s stage=%s attempt=%s elapsed=%.2f error_type=%s',a['id'],method,path,stage,attempt+1,time.monotonic()-started,type(e).__name__)
            relay_tokens.pop(origin,None)
            if readonly and attempt==0:
                time.sleep(.3);continue
            reason='响应超时' if isinstance(e,(TimeoutError,socket.timeout)) or isinstance(getattr(e,'reason',None),(TimeoutError,socket.timeout)) else '连接中断或响应异常'
            detail=f'账号服务{reason}（{stage}）；请稍后刷新' if readonly else f'账号服务{reason}（{stage}）；操作结果尚未确认，请刷新核对，勿重复提交'
            raise HTTPException(502,detail) from None

def collector(a):
    if a['state']!='ready':return {**a,'available':False,'devices':[]}
    try:
        state=relay(a,'status')
        if a['platform']=='apple':
            devices=relay(a,'devices');tasks={d['device_id']:d for d in relay(a,'schedule')['tasks']}
            devices=[{**d,**{k:v for k,v in tasks.get(d['id'],{}).items() if k not in ('device_id','device_name')},'pubkey':d.get('public_key_hex'),'registration':'ready'} for d in devices]
            authorized=state.get('location_ready',False)
        else:
            devices=state['devices'];authorized=state.get('authorized',False)
            active={j.get('device_id') for j in state.get('location_jobs',[]) if j.get('id') in state.get('running_jobs',[]) and j.get('operation')=='locate'}
            devices=[{**d,'running':d['id'] in active} for d in devices]
        return {**a,'available':True,'authorized':authorized,'email':state.get('account'),'auth_state':state.get('state'),'devices':devices}
    except HTTPException as e:return {**a,'available':False,'error':e.detail,'devices':[]}

def mac(pub):
    if not pub or not re.fullmatch('[0-9a-fA-F]{56}',pub):return '--'
    raw=bytearray.fromhex(pub[:12]);raw[0]|=0xC0;return raw.hex().upper()

def latest(con,sn,source,ident):
    where,args=('sn=?',(sn,)) if sn else ('source=? AND device_id=?',(source,ident))
    row=con.execute('SELECT * FROM locations WHERE '+where+' ORDER BY timestamp DESC,id DESC LIMIT 1',args).fetchone()
    if not row:return None
    point=json.loads(row['raw_json']);point.update(timestamp=row['timestamp'],latitude=row['latitude'],longitude=row['longitude'],platform=row['platform'],location_id=row['id'])
    addr=con.execute(db.ADDRESS_SELECT+' WHERE l.id=?',(row['id'],)).fetchone()
    resolved=db.address_data(addr);point.update(address_status=resolved['status'],address=resolved['address']);return point

def apple_battery(con,sn,source,ident):
    where,args=('sn=?',(sn,)) if sn else ('source=? AND device_id=?',(source,ident))
    row=con.execute("SELECT status,timestamp FROM locations WHERE "+where+" AND platform='apple' ORDER BY timestamp DESC,id DESC LIMIT 1",args).fetchone()
    if not row:return None
    status=row['status']
    if isinstance(status,bool) or not isinstance(status,int) or not 0<=status<=255:return None
    code=status&15
    return {'percent':100-code*10 if code<10 else 5,'critical':code>=11,'code':code,'status':status,'timestamp':row['timestamp'],'source':'apple'}


def snapshot():
    parts=control.projections(accounts())
    names=control.names()
    merged={}
    for a in parts:
        for d in a['devices']:
            sn=d.get('sn') or '';key='sn:'+sn if sn else a['id']+'~'+d['id']
            item=merged.setdefault(key,{'id':key,'sn':sn,'name':d['name'],'pubkey':d.get('pubkey'),'members':[]})
            if not item['pubkey']:item['pubkey']=d.get('pubkey')
            if d.get('pubkey') and item['pubkey'] and d['pubkey'].upper()!=item['pubkey'].upper():item['identity_conflict']=True
            item['members'].append({**d,'account_id':a['id'],'account_label':a['label'],'platform':a['platform'],'source':a['source']})
    with db.connect() as con:
        for item in merged.values():
            override=names.get(item['id'])
            if override:
                item['name']=override['name']
                item['name_sync_pending']=any(m['name']!=override['name'] for m in item['members'])
                item['name_sync_error']=override['error'] if item['name_sync_pending'] else None
                for m in item['members']:m['name']=override['name']
            member=item['members'][0]
            item['mac']=mac(item['pubkey']) if item['sn'] else '--'
            item['latest']=latest(con,item['sn'],member['source'],member['id'])
            item['battery']=apple_battery(con,item['sn'],member['source'],member['id'])
            burn=con.execute('SELECT state FROM burn_registry WHERE sn=?',(item['sn'],)).fetchone()
            item['burn_state']=burn[0] if burn else 'unknown'
    return {'accounts':[{k:v for k,v in a.items() if k!='devices'} for a in parts],'devices':list(merged.values())}

def invalidate():
    # Compatibility for import handlers: every web read now uses the DB.
    pass

async def get_snapshot():
    return await asyncio.to_thread(snapshot)

async def display_snapshot():
    return await get_snapshot()


async def import_loop():
    while True:
        with db.connect() as con:job=con.execute("SELECT * FROM console_imports WHERE state IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
        if not job:await asyncio.sleep(2);continue
        result=json.loads(job['results']);items=json.loads(job['items'])
        with db.connect() as con:con.execute("UPDATE console_imports SET state='running' WHERE id=?",(job['id'],))
        for sn in items:
            if any(r['sn']==sn for r in result):continue
            try:
                a=account(job['account']);value=await asyncio.to_thread(relay,a,'keys/import-sn',{'sn':sn,'enabled':bool(job['enabled'])})
                result.append({'sn':sn,'ok':True,'created':value['created'],'name':value['device']['name']})
                with db.connect() as con:con.execute("INSERT OR IGNORE INTO burn_registry VALUES(?,'unknown',NULL,?,'SN import; physical state unknown')",(sn,time.time()))
            except Exception as e:result.append({'sn':sn,'ok':False,'error':e.detail if isinstance(e,HTTPException) else '导入失败，请核对后重试'})
            with db.connect() as con:con.execute('UPDATE console_imports SET results=?,updated_at=? WHERE id=?',(json.dumps(result,ensure_ascii=False),time.time(),job['id']))
            invalidate()
        with db.connect() as con:con.execute("UPDATE console_imports SET state='complete',updated_at=? WHERE id=?",(time.time(),job['id']))

@asynccontextmanager
async def lifespan(app):
    initialize()
    yield

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
install_polling(app);db.install(app);dual_add.install(app,sys.modules[__name__])

@app.middleware('http')
async def guard(request,call_next):
    if request.url.path=='/view' or request.url.path.startswith('/view/'):
        allowed={'/view','/view/','/view/devices','/view/viewer.js','/view/base.css','/view/console.css','/view/ubuntu-theme.css','/view/track','/view/unified-track.js','/view/unified-track.css','/view/time.js'}
        if request.method not in ('GET','HEAD') or request.url.path not in allowed:
            return JSONResponse({'detail':'只读入口不允许此操作'},403)
    origin=request.headers.get('origin')
    if origin and origin not in ORIGINS:return JSONResponse({'detail':'不允许跨站访问'},403)
    if request.headers.get('host','').split(':')[0] not in ('127.0.0.1','localhost','testserver'):return JSONResponse({'detail':'无效主机'},400)
    if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('x-local-token',''),TOKEN):return JSONResponse({'detail':'请刷新页面后重试'},403)
    if int(request.headers.get('content-length','0'))>2_000_000:return JSONResponse({'detail':'请求过大'},413)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Frame-Options':'DENY','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':f"default-src 'self'; connect-src 'self' http://127.0.0.1:18765; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https://a.tile.openstreetmap.fr; frame-src {MAP}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response

@app.exception_handler(RequestValidationError)
async def validation(request,error):return JSONResponse({'detail':'输入格式错误，请检查必填项'},422)

@app.exception_handler(Exception)
async def failure(request,error):return JSONResponse({'detail':'操作未完成，请核对服务状态后重试'},500)

@app.get('/',response_class=HTMLResponse)
def index():return (ROOT/'static/index.html').read_text(encoding='utf-8').replace('__TOKEN__',TOKEN).replace('__MAP__',MAP)

@app.get('/static/{name}')
def static(name:str):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+\.(js|css)',name) or not (ROOT/'static'/name).is_file():raise HTTPException(404)
    return FileResponse(ROOT/'static'/name)


def public_devices(snapshot):
    # Explicit projection: never expose account identities, credentials or tag keys.
    result=[]
    for d in snapshot.get('devices',[]):
        p=d.get('latest') or {};address=p.get('address')
        if isinstance(address,dict):address=address.get('address') or address.get('display_name') or address.get('name')
        point={k:p.get(k) for k in ('timestamp','latitude','longitude','accuracy_m','platform','status')} if p else None
        if point is not None:point['address']=address if isinstance(address,str) else None
        battery=d.get('battery') or {}
        result.append({**{k:d.get(k) for k in ('name','sn','mac')},'latest':point,'battery':{k:battery.get(k) for k in ('percent','critical')} if battery else None})
    return {'devices':result,'readonly':True,'refreshing':snapshot.get('refreshing',False)}

@app.get('/view',response_class=HTMLResponse)
@app.get('/view/',response_class=HTMLResponse)
def viewer_index():return (ROOT/'static/viewer.html').read_text(encoding='utf-8').replace('__MAP__',MAP)

@app.get('/view/devices')
async def viewer_devices():return public_devices(await display_snapshot())

@app.get('/view/track')
def viewer_track(sn:str,limit:int=10,cursor:str='',start:float|None=None,end:float|None=None,platform:str='all'):
    if not re.fullmatch('[0-9]{12}',sn):raise HTTPException(422,'SN格式无效')
    result=db.read_track('apple',sn,limit=limit,cursor=cursor,start=start,end=end,platform=platform)
    # Return only public location fields, never raw report metadata or identities.
    fields=('location_id','timestamp','latitude','longitude','accuracy_m','altitude','status','platform','source','source_label','address_status')
    reports=[]
    for point in result['reports']:
        row={k:point[k] for k in fields if k in point}
        address=point.get('reverse_address')
        if isinstance(address,dict):row['reverse_address']={k:address[k] for k in ('found','address','provider') if k in address}
        reports.append(row)
    return {**{k:result[k] for k in ('sn','total','has_more','next_cursor','earliest','latest')},'reports':reports,'warnings':[]}

@app.get('/view/{name}')
def viewer_asset(name:str):
    if name not in ('viewer.js','base.css','console.css','ubuntu-theme.css','unified-track.js','unified-track.css','time.js'):raise HTTPException(404)
    return FileResponse(ROOT/'static'/name)

@app.get('/api/console')
async def overview():return await display_snapshot()

class NewAccount(BaseModel):
    platform:str=Field(pattern='^(apple|google)$')
    label:str=Field(min_length=1,max_length=60)

@app.post('/api/accounts')
def new_account(body:NewAccount):
    if not body.label.strip():raise HTTPException(422,'账号名称不能为空')
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        rows=con.execute('SELECT id,port FROM managed_accounts').fetchall()
        index=max([int(r['id'][len(body.platform):]) for r in rows if re.fullmatch(body.platform+r'\d+',r['id'])]+[0])+1
        ident=body.platform+str(index);port=max([r['port'] for r in rows if r['port']>=19000]+[18999])+1
        if port>19999:raise HTTPException(409,'账号端口范围已用尽')
        con.execute('INSERT INTO managed_accounts VALUES(?,?,?,?,?,?,NULL,?)',(ident,body.platform,ident,port,body.label.strip(),'provisioning',time.time()))
    invalidate();return {'id':ident,'state':'provisioning'}

class RenameDevice(BaseModel):
    device_key:str=Field(min_length=1,max_length=200)
    name:str=Field(min_length=1,max_length=80)

@app.post('/api/devices/rename')
def rename_device(body:RenameDevice):
    name=body.name.strip()
    if not name:raise HTTPException(422,'设备名称不能为空')
    targets=[d for a in control.projections(accounts()) for d in a['devices']
             if ('sn:'+str(d['sn']) if d.get('sn') else a['id']+'~'+d['id'])==body.device_key]
    if not targets:raise HTTPException(404,'设备不存在，请刷新列表')
    control.rename(body.device_key,name)
    return {'ok':True,'name':name,'updated':len(targets),'sync_pending':True}

class RenameAccount(BaseModel):
    label:str=Field(min_length=1,max_length=60)

@app.post('/api/accounts/{ident}/rename')
def rename_account(ident:str,body:RenameAccount):
    label=body.label.strip()
    if not label:raise HTTPException(422,'账号名称不能为空')
    with db.connect() as con:
        changed=con.execute('UPDATE managed_accounts SET label=? WHERE id=?',(label,ident))
        if not changed.rowcount:raise HTTPException(404,'账号不存在')
    invalidate()
    return {'id':ident,'label':label}

@app.get('/api/accounts/{ident}/authorization-helper')
def authorization_helper(ident:str):
    a=account(ident)
    if a['platform']!='google':raise HTTPException(404)
    import io,zipfile
    bundle=ROOT/'authorization-base.zip'
    if not bundle.is_file():raise HTTPException(503,'授权工具尚未准备好')
    output=io.BytesIO()
    with zipfile.ZipFile(bundle) as src,zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as dest:
        for name in src.namelist():dest.writestr(name,src.read(name))
        dest.writestr('target.json',json.dumps({'base':'https://47.106.211.14','account_id':ident}))
    return Response(output.getvalue(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename="{ident}-authorize.zip"'})

@app.api_route('/api/accounts/{ident}/proxy/{path:path}',methods=['GET','POST','DELETE'])
async def proxy(ident:str,path:str,request:Request):
    a=account(ident);method=request.method
    allowed=(APPLE_READ if method=='GET' else APPLE_WRITE) if a['platform']=='apple' else (GOOGLE_READ if method=='GET' else GOOGLE_WRITE)
    if method=='DELETE':valid=a['platform']=='apple' and bool(re.fullmatch('devices/[A-Za-z0-9_-]+',path))
    else:valid=bool(allowed.fullmatch(path))
    if not valid:raise HTTPException(404)
    query=list(request.query_params.multi_items())
    if any(k!='device_id' or not re.fullmatch('[A-Za-z0-9_-]+',v) for k,v in query):raise HTTPException(422)
    body=await request.json() if method=='POST' else None
    if a['platform']=='google' and path=='devices' and method=='POST':
        # Random globally checked SN avoids per-account allocation collisions.
        if not body.get('sn'):
            occupied={d['sn'] for d in (await get_snapshot())['devices']}
            while True:
                sn=str(secrets.randbelow(900000000000)+100000000000)
                if sn not in occupied:break
            body['sn']=sn
        for d in (await get_snapshot())['devices']:
            if d['sn']==body['sn'] and any(m['platform']=='google' and m['account_id']!=ident for m in d['members']):raise HTTPException(409,'此 SN 已属于其他 Google 账号')
    from urllib.parse import urlencode
    ident=control.submit(a['id'],path+('?' + urlencode(query) if query else ''),body,method)
    if request.headers.get('x-control-async')=='1':
        return JSONResponse({'control_task':ident},status_code=202)
    # Compatibility for existing authorization helpers. This awaits database
    # state asynchronously; no collector connection or lock exists in the web.
    for _ in range(600):
        task=await asyncio.to_thread(control.read,ident)
        if task['state'] not in ('queued','running'):
            return JSONResponse(task['result'],status_code=task['code'] or 500)
        await asyncio.sleep(.5)
    return JSONResponse({'detail':'后台任务仍在处理，请勿重复提交','control_task':ident},status_code=202)

@app.get('/api/control-tasks/{ident}')
def control_task(ident:str):return control.read(ident)


@app.get('/api/unified-track')
def track(sn:str='',account_id:str='',device_id:str='',limit:int|None=None,cursor:str='',start:float|None=None,end:float|None=None,platform:str='all'):
    if sn and not re.fullmatch('[0-9]{12}',sn):raise HTTPException(422)
    if not sn and not(account_id and device_id):raise HTTPException(422)
    source=account(account_id)['source'] if not sn else 'apple'
    return db.read_track(source,sn,device_id,limit,cursor,start,end,platform)

class SnImport(BaseModel):
    account_id:str
    sns:str=Field(min_length=1,max_length=20000)
    enabled:bool=True

@app.post('/api/import-sn')
def import_sns(body:SnImport):
    a=account(body.account_id)
    if a['platform']!='apple':raise HTTPException(422,'请选择 Apple 账号')
    values=list(dict.fromkeys(re.split(r'[\s,，;；]+',body.sns.strip())))
    if not values or len(values)>1000 or any(not re.fullmatch('[0-9]{12}',v) for v in values):raise HTTPException(422,'每个 SN 须为12位数字，一次最多1000个')
    ident=str(uuid.uuid4())
    with db.connect() as con:
        current=con.execute("SELECT id FROM console_imports WHERE account=? AND items=? AND state IN ('queued','running')",(a['id'],json.dumps(values))).fetchone()
        if current:return {'id':current[0]}
        con.execute('INSERT INTO console_imports VALUES(?,?,?,?,?,?,?,?)',(ident,a['id'],json.dumps(values),int(body.enabled),'queued','[]',time.time(),time.time()))
    return {'id':ident}

@app.get('/api/import-sn/{ident}')
def import_result(ident:str):
    with db.connect() as con:row=con.execute('SELECT * FROM console_imports WHERE id=?',(ident,)).fetchone()
    if not row:raise HTTPException(404)
    return {'id':ident,'state':row['state'],'total':len(json.loads(row['items'])),'results':json.loads(row['results'])}

class BurnInput(BaseModel):
    sn:str=Field(pattern=r'^[0-9]{12}$')
    session:str=Field(min_length=16,max_length=80)
    ack:str=''

@app.post('/api/burn/{action}')
def burn(action:str,body:BurnInput):
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT * FROM burn_registry WHERE sn=?',(body.sn,)).fetchone()
        if not row:raise HTTPException(409,'设备烧录状态未知，请先核对实物')
        if action=='claim':
            if row['state']!='unburned':raise HTTPException(409,'设备已烧录或被其他会话占用，请重新加载队列')
            con.execute("UPDATE burn_registry SET state='pending',session=?,updated_at=?,evidence='serial claim' WHERE sn=?",(body.session,time.time(),body.sn))
        elif action=='complete':
            if row['session']!=body.session or row['state'] not in ('pending','burned') or body.ack!='OK+SETKEY=sn:'+body.sn:raise HTTPException(409,'烧录确认与当前会话或 SN 不匹配')
            con.execute("UPDATE burn_registry SET state='burned',updated_at=?,evidence=? WHERE sn=?",(time.time(),body.ack,body.sn))
        elif action=='reset':
            if body.ack!='CONFIRM_UNBURNED:'+body.sn:raise HTTPException(422,'请明确确认实物未烧录')
            con.execute("UPDATE burn_registry SET state='unburned',session=NULL,updated_at=?,evidence='operator confirmed unburned' WHERE sn=?",(time.time(),body.sn))
        else:raise HTTPException(404)
    invalidate();return {'ok':True}
