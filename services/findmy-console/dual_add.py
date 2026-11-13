"""Durable paired-platform adds. No private keys are stored in this queue."""
import asyncio,base64,csv,io,json,re,time,uuid,zipfile
from fastapi import HTTPException
from pydantic import BaseModel,Field
import central_locations as db

host=None
lock=asyncio.Lock()
def initialize():
    with db.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS paired_adds(id TEXT PRIMARY KEY,state TEXT,request TEXT,items TEXT,created_at REAL,updated_at REAL)')
def read(ident):
    with db.connect() as c:r=c.execute('SELECT * FROM paired_adds WHERE id=?',(ident,)).fetchone()
    if not r:raise HTTPException(404,'任务不存在')
    return {**dict(r),'request':json.loads(r['request']),'items':json.loads(r['items'])}
def save(job):
    with db.connect() as c:c.execute('UPDATE paired_adds SET state=?,items=?,updated_at=? WHERE id=?',(job['state'],json.dumps(job['items'],ensure_ascii=False),time.time(),job['id']))
    host.invalidate()
def pending():
    with db.connect() as c:rows=c.execute("SELECT items FROM paired_adds WHERE state IN ('queued','running')").fetchall()
    return [item for r in rows for item in json.loads(r[0])]
def options(snapshot):
    reserved=pending();result=[]
    for a in snapshot['accounts']:
        used={d['sn'] or d['id'] for d in snapshot['devices'] for m in d['members'] if m['account_id']==a['id']}
        held={i['sn'] for i in reserved if i.get('google_account')==a['id'] and i['google']!='done'}-used
        result.append({**a,'used':len(used),'reserved':len(held),'free':max(0,100-len(used)-len(held)) if a['platform']=='google' else None})
    return result
def normalize(rows):
    if not 1<=len(rows)<=500:raise HTTPException(422,'一次请输入 1–500 台设备')
    seen=set();result=[]
    for n,row in enumerate(rows,1):
        sn=str(row.get('sn') or '').strip();pub=str(row.get('pubkey') or '').strip().upper();name=str(row.get('name') or '').strip() or sn
        error=''
        if not re.fullmatch('[1-9][0-9]{11}',sn):error='SN 须为12位数字'
        elif not re.fullmatch('[0-9A-F]{56}',pub) or pub=='0'*56:error='pubkey 须为56位非全零十六进制'
        elif len(name)>80:error='名称最多80个字符'
        elif sn in seen:error='本批 SN 重复'
        seen.add(sn);result.append({'row':n,'sn':sn,'pubkey':pub,'name':name,'error':error,'apple':'pending','google':'pending'})
    return result

def plan(body,snapshot):
    rows=normalize(body['rows']);accounts={a['id']:a for a in options(snapshot)}
    if any(a['state']=='ready' and not a['available'] for a in accounts.values()):raise HTTPException(409,'有账号暂时不可用，无法可靠核对重复 SN 和容量，请稍后预览')
    def ready(ident,platform):
        a=accounts.get(ident)
        return bool(a and a['platform']==platform and a['available'] and a.get('authorized'))
    if not ready(body['apple_account'],'apple') or not ready(body['google_account'],'google'):raise HTTPException(409,'请选择已授权、可用的 Apple 和 Google 账号')
    occupied={i['sn'] for i in pending()};devices={d['sn']:d for d in snapshot['devices'] if d['sn']}
    google=[body['google_account']]+[a for a in accounts if a!=body['google_account'] and ready(a,'google')]
    if not body['auto_allocate']:google=google[:1]
    for row in rows:
        row.update(apple_account=body['apple_account'],google_account=body['google_account'],apple_reused=False,google_reused=False)
        if row['error']:continue
        if row['sn'] in occupied:row['error']='该 SN 已在另一个添加任务中';continue
        old=devices.get(row['sn']);members=old['members'] if old else []
        if any(m.get('pubkey') and m['pubkey'].upper()!=row['pubkey'] for m in members):row['error']='已有设备公钥不一致，停止添加';continue
        for platform in ['apple','google']:
            existing=[m for m in members if m['platform']==platform]
            if len(existing)>1:row['error']='同平台有多个同 SN 设备，请先核对';continue
            if existing:
                m=existing[0];row[platform+'_account']=m['account_id'];row[platform+'_id']=m['id'];row[platform+'_reused']=True
                if not ready(m['account_id'],platform):row['error']='已有设备所属账号尚未授权'
        if not row['google_reused']:
            chosen=next((a for a in google if accounts[a]['free']>0),None)
            if not chosen:row['error']='Google 可用容量不足'
            else:row['google_account']=chosen;accounts[chosen]['free']-=1
    return rows

class AddInput(BaseModel):
    apple_account:str
    google_account:str
    rows:list[dict]=Field(min_length=1,max_length=500)
    auto_allocate:bool=True
    unburned:bool=False
class FileInput(BaseModel):
    filename:str=Field(max_length=200)
    content:str=Field(max_length=1400000)

def parse_file(body):
    try:raw=base64.b64decode(body.content,validate=True)
    except Exception:raise HTTPException(422,'文件内容无效')
    if len(raw)>1_000_000:raise HTTPException(422,'文件最大1MB')
    suffix=body.filename.lower().rsplit('.',1)[-1]
    try:
        if suffix=='xlsx':
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if len(z.infolist())>300 or sum(x.file_size for x in z.infolist())>12_000_000:raise ValueError()
            import openpyxl
            wb=openpyxl.load_workbook(io.BytesIO(raw),read_only=True,data_only=False,keep_links=False)
            try:
                sheets=[s for s in wb if s.sheet_state=='visible'];sheet=sheets[0]
                values=list(sheet.iter_rows(max_row=502,max_col=20,values_only=True))
            finally:wb.close()
        elif suffix in ('csv','tsv','txt'):
            try:text=raw.decode('utf-8-sig')
            except UnicodeDecodeError:text=raw.decode('gb18030')
            delimiter='\t' if '\t' in text.splitlines()[0] else ','
            values=list(csv.reader(io.StringIO(text),delimiter=delimiter))
        else:raise HTTPException(422,'支持 .xlsx、.csv、.tsv；旧版 .xls 请另存为 .xlsx')
        values=[r for r in values if any(v is not None and str(v).strip() for v in r)]
        if not values:raise ValueError()
        aliases={'sn':'sn','设备sn':'sn','序列号':'sn','pubkey':'pubkey','key':'pubkey','公钥':'pubkey','apple公钥':'pubkey','名称':'name','设备名称':'name','name':'name'}
        header={aliases[str(v).strip().lower()]:i for i,v in enumerate(values[0]) if str(v).strip().lower() in aliases}
        if 'sn' in header and 'pubkey' in header:values=values[1:]
        else:header={'sn':0,'pubkey':1,'name':2}
        def cell(row,i):
            v=row[i] if i<len(row) else ''
            if isinstance(v,float) and v.is_integer():v=int(v)
            return '' if v is None else str(v).strip()
        rows=[{k:cell(row,i) for k,i in header.items()} for row in values]
        if not 1<=len(rows)<=500:raise HTTPException(422,'表格须含1–500台设备，读取第一个可见工作表')
        return {'rows':rows}
    except HTTPException:raise
    except Exception:raise HTTPException(422,'无法读取表格，请检查格式、表头和文件大小') from None

async def call(ident,path,body=None):return await asyncio.to_thread(host.relay,host.account(ident),path,body)
async def process(job,row):
    if row['apple']=='pending':
        try:
            result=await call(row['apple_account'],'keys/import-sn',{'sn':row['sn'],'expected_pubkey':row['pubkey'],'name':row['name'],'enabled':True})
            row['apple_id']=result['device']['id'];row['apple']='done';row.pop('apple_error',None)
        except HTTPException as e:
            row['apple']='error';row['apple_error']=e.detail
            if row['google']!='done' and e.status_code in (409,422) and '公钥' in str(e.detail):row['google']='error';row['google_error']='Apple 公钥核对不一致，未操作 Google'
        save(job)
    if row['google'] in ('done','error'):return
    try:
        a=row['google_account'];status=await call(a,'status');d=next((d for d in status['devices'] if d.get('sn')==row['sn']),None)
        if d and d.get('pubkey') and d['pubkey'].upper()!=row['pubkey']:raise HTTPException(409,'Google 同 SN 公钥冲突')
        if not d:
            if row.get('google_id'):raise HTTPException(409,'原 Google 设备已不存在，请核对，未重新生成密钥')
            d=await call(a,'devices',{'sn':row['sn'],'name':row['name'],'pubkey':row['pubkey'],'cid':'3A105789'})
        row['google_id']=d['id'];save(job)
        if not d.get('pubkey'):await call(a,'devices/'+d['id']+'/burn-config',{'cid':d.get('cid') or '3A105789','pubkey':row['pubkey']})
        if d['name']!=row['name']:await call(a,'devices/'+d['id']+'/rename',{'name':row['name']})
        if d.get('registration')=='registered':
            await call(a,'devices/'+d['id']+'/schedule',{'enabled':True});row['google']='done';row.pop('google_error',None)
            with db.connect() as c:c.execute('INSERT OR IGNORE INTO burn_registry VALUES(?,?,NULL,?,?)',(row['sn'],'unburned' if job['request']['unburned'] else 'unknown',time.time(),'paired add; user burn declaration'))
        else:
            matching=[j for j in status.get('jobs',[]) if j.get('device_id')==d['id'] and j.get('operation')=='register']
            running=next((j for j in matching if j['state'] in ('queued','running')),None)
            known=next((j for j in matching if j['id']==row.get('register_job')),None)
            if running:row['register_job']=running['id'];row.setdefault('register_at',time.time());row['google']='registering'
            elif row.get('register_attempted'):
                if known and known['state'] in ('failed','interrupted'):raise HTTPException(409,'Google 注册未完成：'+str(known.get('error') or known['state']))
                if known and known['state']=='success':raise HTTPException(409,'注册任务已结束但设备状态未确认，请核对后重试')
                raise HTTPException(409,'注册提交结果未确认，未重复提交；请先同步 Google 设备再重试')
            else:
                row['register_attempted']=True;row['register_at']=time.time();row['google']='registering';save(job)
                try:j=await call(a,'jobs/register?device_id='+d['id'],{})
                except HTTPException as e:
                    if e.status_code in (400,401,403,404,409,422,429):row.pop('register_attempted',None)
                    raise
                row['register_job']=j['id']
            if time.time()-row['register_at']>900:raise HTTPException(409,'注册等待超过15分钟，请核对后重试')
    except HTTPException as e:row['google']='error';row['google_error']=e.detail
    save(job)

async def worker():
    while True:
        r=None;row=None;job=None
        try:
            with db.connect() as c:r=c.execute("SELECT id FROM paired_adds WHERE state IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            if r:
                job=read(r[0]);job['state']='running'
                row=next((i for i in job['items'] if i['apple']=='pending' or i['google'] not in ('done','error')),None)
                if row:await process(job,row)
                if all(i['apple'] in ('done','error') and i['google'] in ('done','error') for i in job['items']):job['state']='complete';save(job)
        except asyncio.CancelledError:raise
        except Exception:
            # Mark the current row rather than endlessly retrying mutations.
            if job:
                for platform in ('apple','google'):
                    if row and row[platform] not in ('done','error'):row[platform]='error';row[platform+'_error']='处理异常，请核对后重试'
                save(job)
        await asyncio.sleep(3)

def install(app,module):
    global host
    host=module
    @app.post('/api/paired-add/file')
    def file(body:FileInput):return parse_file(body)
    @app.get('/api/paired-add/options')
    async def opts():return options(await host.get_snapshot())
    @app.post('/api/paired-add/preview')
    async def preview(body:AddInput):
        async with lock:
            snapshot=await host.get_snapshot();data=body.model_dump();items=plan(data,snapshot);ident=str(uuid.uuid4())
            with db.connect() as c:c.execute('INSERT INTO paired_adds VALUES(?,?,?,?,?,?)',(ident,'preview',json.dumps(data),json.dumps(items,ensure_ascii=False),time.time(),time.time()))
            return read(ident)
    @app.get('/api/paired-add')
    def jobs():
        with db.connect() as c:rows=c.execute("SELECT id FROM paired_adds WHERE state!='preview' ORDER BY created_at DESC LIMIT 10").fetchall()
        return [read(r[0]) for r in rows]
    @app.get('/api/paired-add/{ident}')
    def get(ident:str):return read(ident)
    @app.post('/api/paired-add/{ident}/start')
    async def start(ident:str):
        async with lock:
            job=read(ident)
            if job['state']!='preview':return job
            host.invalidate();items=plan(job['request'],await host.get_snapshot())
            if any(i['error'] for i in items):raise HTTPException(409,'预览中存在错误或容量已变化，请重新预览')
            if [(i['apple_account'],i['google_account']) for i in items]!=[(i['apple_account'],i['google_account']) for i in job['items']]:raise HTTPException(409,'账号分配已变化，请重新预览确认')
            job['items']=items;job['state']='queued';save(job);return job
    @app.post('/api/paired-add/{ident}/retry')
    async def retry(ident:str):
        async with lock:
            job=read(ident)
            if job['state']!='complete':raise HTTPException(409,'请等待当前任务结束')
            busy={i['sn'] for i in pending()}
            if any(i['sn'] in busy for i in job['items']):raise HTTPException(409,'有同 SN 任务正在执行，请稍后重试')
            for i in job['items']:
                if i['apple']=='error':i['apple']='pending'
                if i['google']=='error':
                    # Explicit retry may retry a confirmed failure, but never an uncertain submission.
                    if i.get('register_job'):
                        st=await call(i['google_account'],'status');j=next((j for j in st.get('jobs',[]) if j['id']==i['register_job']),None)
                        if j and j['state']=='failed':i.pop('register_attempted',None);i.pop('register_job',None)
                    i['google']='pending'
            job['state']='queued';save(job);return job
