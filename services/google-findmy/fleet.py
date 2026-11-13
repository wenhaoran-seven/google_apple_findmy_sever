"""Authenticated facade for the three existing isolated Google services."""
import asyncio
import json
import re
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode, parse_qsl
from fastapi import HTTPException, Request
from fastapi.responses import Response

ACCOUNTS = {'google1':8767,'google2':8769,'google3':8771}
LABELS = {key:'Google 账号'+key[-1] for key in ACCOUNTS}
READ = re.compile(r'(?:status|unified-track|devices/[A-Za-z0-9_-]+/(?:history|burn-command|burn-config|firmware|identity|export))\Z')
WRITE = re.compile(r'(?:devices|auth/import|network|jobs/(?:sync|renew|locate|register)|devices/[A-Za-z0-9_-]+/(?:burn-config|address|delete|rename|configure|schedule))\Z')

def permitted(method,path):
    return bool((READ if method=='GET' else WRITE if method=='POST' else re.compile(r'(?!)')).fullmatch(path))

def relay(account, method, path, query='', body=None):
    origin=f'http://127.0.0.1:{ACCOUNTS[account]}'
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(origin+'/',timeout=5) as r: page=r.read(500_000).decode()
    token=re.search(r'name="local-token" content="([^"]+)"',page)
    if not token:raise RuntimeError('Account page unavailable')
    headers={'X-Local-Token':token[1],'Content-Type':'application/json'}
    req=urllib.request.Request(origin+'/api/'+path+('?' + query if query else ''),headers=headers,data=body,method=method)
    try:
        with opener.open(req,timeout=60) as r:return r.status,r.read(),r.headers.get('Content-Type','application/json')
    except urllib.error.HTTPError as e:
        return e.code,e.read(),e.headers.get('Content-Type','application/json')

def namespaced(account,data):
    def qualify(value):return account+'~'+value if value else value
    devices=[{**d,'id':qualify(d['id']),'native_id':d['id'],'account_id':account,'account_label':LABELS[account]} for d in data.get('devices',[])]
    def jobs(values):return [{**j,'id':qualify(j.get('id')),'device_id':qualify(j.get('device_id')),'account_id':account,'account_label':LABELS[account]} for j in values]
    return {**data,'devices':devices,'jobs':jobs(data.get('jobs',[])),'location_jobs':jobs(data.get('location_jobs',[])),
            'running_job':qualify(data.get('running_job')),
            'running_jobs':[qualify(v) if isinstance(v,str) else {**v,'account_id':account} for v in data.get('running_jobs',[])]}

def merged(results):
    accounts=[];devices=[];jobs=[];locations=[];running=[]
    for account,result in results:
        if isinstance(result,BaseException):
            accounts.append({'id':account,'label':LABELS[account],'available':False,'authorized':False,'error':'读取失败，请重试；该账号数据未计入当前汇总。'})
            continue
        info=namespaced(account,result)
        accounts.append({'id':account,'label':LABELS[account],'available':True,'authorized':bool(info.get('authorized')),
                         'device_count':len(info['devices']),'last_sync':info.get('last_sync'),'renewal':info.get('renewal',{}),
                         'cloud_count':len(info.get('cloud_devices',[])),'running_count':len(info.get('running_jobs',[])),
                         'query_cooldown_until':info.get('query_cooldown_until',0)})
        devices.extend(info['devices']);jobs.extend(info['jobs']);locations.extend(info['location_jobs']);running.extend(info['running_jobs'])
    jobs.sort(key=lambda j:j.get('created_at',0),reverse=True);locations.sort(key=lambda j:j.get('created_at',0),reverse=True)
    return {'accounts':accounts,'devices':devices,'jobs':jobs,'location_jobs':locations,'running_jobs':running,
            'running_job':None,'authorized':all(a['authorized'] for a in accounts),'renewal':{},'cloud_devices':[],
            'network_mode':'server_proxy','query_concurrency':30,'query_cooldown_until':0}

def install(app):
    # Avoid overlapping poll fan-out; never retry a write automatically.
    lock=asyncio.Lock();cached=None;cached_at=0;revision=0
    @app.get('/api/fleet/status')
    async def fleet_status():
        nonlocal cached,cached_at
        async with lock:
            if cached is not None and time.monotonic()-cached_at<3:return cached
            started_revision=revision
            def fetch(account):
                code,body,_=relay(account,'GET','status')
                if code!=200:raise RuntimeError('Account status unavailable')
                return json.loads(body)
            values=await asyncio.gather(*(asyncio.to_thread(fetch,key) for key in ACCOUNTS),return_exceptions=True)
            result=merged(zip(ACCOUNTS,values))
            if revision==started_revision:cached=result;cached_at=time.monotonic()
            return result

    @app.api_route('/api/accounts/{account}/{path:path}',methods=['GET','POST'])
    async def account_api(account:str,path:str,request:Request):
        nonlocal cached,cached_at,revision
        if account not in ACCOUNTS or not permitted(request.method,path):raise HTTPException(404,'不支持的账号或操作')
        pairs=list(request.query_params.multi_items())
        allowed={'device_id','sn','start','end','format'}
        if any(k not in allowed for k,v in pairs):raise HTTPException(422,'不支持的查询参数')
        body=await request.body() if request.method=='POST' else None
        if body is not None and len(body)>2_000_000:raise HTTPException(413,'请求过大')
        if request.method=='POST':revision+=1;cached=None;cached_at=0
        try:
            status,content,content_type=await asyncio.to_thread(relay,account,request.method,path,urlencode(pairs),body)
        except Exception:
            raise HTTPException(502, LABELS[account]+'连接失败；操作结果未确认，请刷新核对后再决定是否重试。') from None
        finally:
            if request.method=='POST':revision+=1;cached=None;cached_at=0
        return Response(content=content,status_code=status,media_type=content_type.split(';')[0])
