"""Shared SQLite location archive. No login credentials or tag keys are stored."""
import asyncio
from contextlib import contextmanager, closing
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
import urllib.request
import urllib.error

LABELS={'apple':'Apple','google1':'Google 账号1','google2':'Google 账号2','google3':'Google 账号3'}
def source_label(source):
    if source in LABELS:return LABELS[source]
    return ('Apple 账号' if source.startswith('apple') else 'Google 账号')+re.sub(r'[^0-9]','',source)

FIELDS={'timestamp','latitude','longitude','accuracy_m','altitude','status','is_own_report','confidence','name','unknown_11','unknown_11_received_at','unknown_11_received_beijing'}
SCHEMA='''
CREATE TABLE IF NOT EXISTS sources(source TEXT PRIMARY KEY,platform TEXT,account_name TEXT,label TEXT,last_write_at REAL,backfill_at REAL);
CREATE TABLE IF NOT EXISTS devices(source TEXT,device_id TEXT,sn TEXT,device_name TEXT,archived INTEGER,updated_at REAL,PRIMARY KEY(source,device_id));
CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY,source TEXT NOT NULL,platform TEXT NOT NULL,account_name TEXT,sn TEXT,device_id TEXT NOT NULL,device_name TEXT,
 timestamp REAL NOT NULL,latitude REAL NOT NULL,longitude REAL NOT NULL,altitude REAL,accuracy_m REAL,status INTEGER,is_own_report INTEGER,unknown_11 TEXT,
 fingerprint TEXT NOT NULL,raw_json TEXT NOT NULL,first_seen_at REAL NOT NULL,last_seen_at REAL NOT NULL,UNIQUE(source,device_id,fingerprint));
CREATE INDEX IF NOT EXISTS locations_sn_time ON locations(sn,timestamp);
CREATE INDEX IF NOT EXISTS locations_source_device_time ON locations(source,device_id,timestamp);
CREATE TABLE IF NOT EXISTS location_versions(location_id INTEGER,raw_hash TEXT,raw_json TEXT,first_seen_at REAL,last_seen_at REAL,PRIMARY KEY(location_id,raw_hash));
CREATE TABLE IF NOT EXISTS rejected_locations(source TEXT,device_id TEXT,raw_hash TEXT,raw_json TEXT,reason TEXT,seen_at REAL,PRIMARY KEY(source,device_id,raw_hash));
CREATE TABLE IF NOT EXISTS geocodes(coordinate_key TEXT PRIMARY KEY,result_json TEXT,expires_at REAL);
CREATE TABLE IF NOT EXISTS geocode_rate(id INTEGER PRIMARY KEY CHECK(id=1),next_at REAL,hour_start REAL,count INTEGER);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);

CREATE TABLE IF NOT EXISTS address_points(
 coordinate_key TEXT PRIMARY KEY,latitude REAL,longitude REAL,location_id INTEGER,
 priority INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending',
 anchor_key TEXT,distance_m REAL,expires_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS address_pending ON address_points(status,next_at,priority);
CREATE INDEX IF NOT EXISTS address_expiry ON address_points(status,expires_at);
CREATE TABLE IF NOT EXISTS location_addresses(location_id INTEGER PRIMARY KEY,coordinate_key TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS address_anchors(coordinate_key TEXT PRIMARY KEY,latitude REAL,longitude REAL);
CREATE INDEX IF NOT EXISTS address_anchor_lat ON address_anchors(latitude);

INSERT OR IGNORE INTO metadata VALUES('schema_version','1');
'''

def database_path():
    configured=os.environ.get('FINDMY_LOCATION_DB')
    if configured:return Path(configured)
    here=Path(__file__).resolve().parent
    project=here.parent if here.name=='localfindmy' or here.name.startswith('google-service') else here
    return project/'shared-data'/'locations.sqlite3'

@contextmanager
def connect():
    con=sqlite3.connect(database_path(),timeout=60)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA busy_timeout=60000')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback();raise
    finally:
        con.close()

def initialize():
    path=database_path();path.parent.mkdir(parents=True,exist_ok=True)
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o660);os.close(fd)
    except FileExistsError:
        pass
    with connect() as con:
        con.execute('PRAGMA journal_mode=WAL');con.executescript(SCHEMA)

def clean(point):
    return {k:v for k,v in point.items() if k in FIELDS or k.startswith('unknown_')}

def normalized(point):
    raw=clean(point)
    value=raw['timestamp']
    if isinstance(value,(int,float)) and not isinstance(value,bool):stamp=float(value)
    else:
        dt=datetime.fromisoformat(value.replace('Z','+00:00'))
        if dt.tzinfo is None:raise ValueError('timestamp has no timezone')
        stamp=dt.timestamp()
    lat,lon=float(raw['latitude']),float(raw['longitude'])
    if not all(math.isfinite(v) for v in (stamp,lat,lon)) or abs(lat)>90 or abs(lon)>180:raise ValueError('invalid coordinate/time')
    stable={**raw,'timestamp':stamp,'latitude':lat,'longitude':lon}
    # Response metadata can change on repeated queries; preserve it in versions.
    for key in list(stable):
        if key.startswith('unknown_'):stable.pop(key)
    fingerprint=hashlib.sha256(json.dumps(stable,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    return raw,stamp,lat,lon,fingerprint

def ingest(source,account,device,points,archived=False):
    now=time.time();device_id=str(device['id']);sn=str(device.get('sn') or '') or None;name=device.get('name','')
    platform='apple' if source.startswith('apple') else 'google';accepted=0;rejected=0
    with connect() as con:
        con.execute('INSERT INTO sources VALUES(?,?,?,?,?,NULL) ON CONFLICT(source) DO UPDATE SET account_name=excluded.account_name,last_write_at=excluded.last_write_at',(source,platform,account,source_label(source),now))
        con.execute('INSERT INTO devices VALUES(?,?,?,?,?,?) ON CONFLICT(source,device_id) DO UPDATE SET sn=excluded.sn,device_name=excluded.device_name,archived=excluded.archived,updated_at=excluded.updated_at',(source,device_id,sn,name,int(archived),now))
        for point in points:
            try:
                raw,stamp,lat,lon,fingerprint=normalized(point)
                raw_json=json.dumps(raw,ensure_ascii=False,sort_keys=True,allow_nan=False)
            except (ValueError,KeyError,TypeError,OverflowError):
                raw_json=json.dumps(clean(point),ensure_ascii=False,sort_keys=True,default=str)
                digest=hashlib.sha256(raw_json.encode()).hexdigest()
                con.execute('INSERT OR IGNORE INTO rejected_locations VALUES(?,?,?,?,?,?)',(source,device_id,digest,raw_json,'invalid timestamp/coordinates',now));rejected+=1;continue
            own=raw.get('is_own_report');unknown=json.dumps(raw['unknown_11'],ensure_ascii=False) if 'unknown_11' in raw else None
            con.execute('''INSERT INTO locations(source,platform,account_name,sn,device_id,device_name,timestamp,latitude,longitude,altitude,accuracy_m,status,is_own_report,unknown_11,fingerprint,raw_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,device_id,fingerprint) DO UPDATE SET
                sn=COALESCE(excluded.sn,locations.sn),device_name=excluded.device_name,account_name=excluded.account_name,unknown_11=excluded.unknown_11,raw_json=excluded.raw_json,last_seen_at=excluded.last_seen_at''',
                (source,platform,account,sn,device_id,name,stamp,lat,lon,raw.get('altitude'),raw.get('accuracy_m'),raw.get('status'),int(own) if isinstance(own,bool) else None,unknown,fingerprint,raw_json,now,now))
            ident=con.execute('SELECT id FROM locations WHERE source=? AND device_id=? AND fingerprint=?',(source,device_id,fingerprint)).fetchone()[0]
            digest=hashlib.sha256(raw_json.encode()).hexdigest()
            con.execute('INSERT INTO location_versions VALUES(?,?,?,?,?) ON CONFLICT(location_id,raw_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at',(ident,digest,raw_json,now,now));accepted+=1
    return {'accepted':accepted,'rejected':rejected}

def account_name(service,source):
    return service.status().get('account') if source.startswith('apple') else service.store.get('auth:username')

def backfill(service,source):
    initialize();account=account_name(service,source);accepted=rejected=0
    if source.startswith('apple'):
        batches=[({'id':ident,**service.devices.get(ident,{'name':history.get('device','')})},history.get('reports',[]),ident not in service.devices) for ident,history in service.location_cache.items()]
        for ident,item in service.devices.items():
            if ident not in service.location_cache:batches.append(({'id':ident,**item},[],False))
    else:
        batches=[(entry['device'],entry.get('history',[]),True) for entry in service.store.all('deleted_device:') if entry.get('device')]
        batches.extend((d,service.store.get('history:'+d['id'],[]),False) for d in service.store.all('device:'))
    for device,points,archived in batches:
        result=ingest(source,account,device,points,archived);accepted+=result['accepted'];rejected+=result['rejected']
    with connect() as con:
        con.execute('INSERT INTO sources VALUES(?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET account_name=excluded.account_name,backfill_at=excluded.backfill_at',(source,'apple' if source.startswith('apple') else 'google',account,source_label(source),time.time(),time.time()))
        con.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('backfill:'+source,json.dumps({'input_points':accepted+rejected,'accepted':accepted,'rejected':rejected,'at':time.time()})))
    return {'accepted':accepted,'rejected':rejected}

def persist(service,source,device,points,history):
    pending=getattr(service,'_central_reconcile',None)
    if pending is None:pending=set();service._central_reconcile=pending
    try:
        result=ingest(source,account_name(service,source),device,history if device['id'] in pending else points)
        pending.discard(device['id']);return result
    except Exception:
        pending.add(device['id']);raise

def coordinate_key(lat,lon):return f'{lat:.7f},{lon:.7f}'

def read_track(source,sn='',device_id=''):
    where,args=('sn=?',(sn,)) if sn else ('source=? AND device_id=?',(source,device_id))
    with connect() as con:
        rows=con.execute('SELECT * FROM locations WHERE '+where+' ORDER BY timestamp,source,device_id,id',args).fetchall()
        addresses={r['id']:address_data(r) for r in con.execute(ADDRESS_SELECT+' WHERE l.'+where.replace(' AND device_id',' AND l.device_id'),args)}
        available=[r[0] for r in con.execute('SELECT source FROM sources WHERE backfill_at IS NOT NULL')]
    reports=[];matches={}
    for row in rows:
        data=json.loads(row['raw_json']);data.update(location_id=row['id'],timestamp=row['timestamp'],latitude=row['latitude'],longitude=row['longitude'],source=row['platform'],platform=row['platform'],source_label='Apple' if row['platform']=='apple' else 'Google',device_id=row['device_id'])
        data['address_status']=addresses[row['id']]['status']
        if addresses[row['id']].get('address'):data['reverse_address']=addresses[row['id']]['address']
        reports.append(data);matches[(row['source'],row['device_id'])]={'id':row['device_id'],'name':row['device_name'],'source':row['source']}
    warnings=[]
    if not sn:warnings.append('未提供 SN，仅显示当前设备，未跨平台合并。')
    missing=set(LABELS)-set(available)
    if missing:warnings.append('历史导入尚未完成：'+'、'.join(source_label(k) for k in sorted(missing)))
    return {'sn':sn or None,'reports':reports,'matches':list(matches.values()),'warnings':warnings,'available_sources':sorted({'apple' if k.startswith('apple') else 'google' for k in available}),'storage':'shared_sqlite'}

def summary():
    with connect() as con:
        return {'total':con.execute('SELECT COUNT(*) FROM locations').fetchone()[0],
                'sources':[dict(r) for r in con.execute('SELECT source,platform,COUNT(*) AS count,MAX(timestamp) AS latest FROM locations GROUP BY source')],
                'rejected':con.execute('SELECT COUNT(*) FROM rejected_locations').fetchone()[0],
                'backfills':{r[0]:json.loads(r[1]) for r in con.execute("SELECT key,value FROM metadata WHERE key LIKE 'backfill:%'")}}

def snapshot_file():
    directory=database_path().parent/'exports';directory.mkdir(exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='locations-',suffix='.sqlite3',dir=directory);os.close(fd)
    try:
        with connect() as src,closing(sqlite3.connect(name)) as dest:
            src.backup(dest,pages=512,sleep=0.02)
            dest.execute('PRAGMA journal_mode=DELETE')
            if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise RuntimeError('Snapshot integrity failed')
        return name
    except Exception:
        Path(name).unlink(missing_ok=True);raise

def geocode_prepare(location_id):
    with connect() as con:
        row=con.execute('SELECT latitude,longitude FROM locations WHERE id=?',(location_id,)).fetchone()
        if not row:raise HTTPException(404,'位置不存在，请重新打开轨迹')
        lat,lon=row;key=coordinate_key(lat,lon);now=time.time()
        cached=con.execute('SELECT result_json FROM geocodes WHERE coordinate_key=? AND expires_at>?',(key,now)).fetchone()
        if cached:return lat,lon,key,{**json.loads(cached[0]),'cached':True}
        con.execute('BEGIN IMMEDIATE')
        rate=con.execute('SELECT * FROM geocode_rate WHERE id=1').fetchone()
        start,count=(rate['hour_start'],rate['count']) if rate and now-rate['hour_start']<3600 else (now,0)
        if rate and now<rate['next_at']:raise HTTPException(429,'查询较频繁，请稍后再试')
        if count>=120:raise HTTPException(429,'本小时地址查询已达上限，已有缓存仍可查看')
        con.execute('INSERT OR REPLACE INTO geocode_rate VALUES(1,?,?,?)',(now+2,start,count+1))
    return lat,lon,key,None

def photon_request(lat,lon):
    from urllib.parse import urlencode
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request=urllib.request.Request('https://photon.komoot.io/reverse?'+urlencode({'lat':lat,'lon':lon,'limit':1,'radius':1}),headers={'Accept':'application/json','User-Agent':'FindMyWorkbench/1.0 (rate-limited reverse lookup)'})
    with opener.open(request,timeout=18) as response:return json.load(response)

async def lookup_address(location_id):
    lat,lon,key,cached=await asyncio.to_thread(geocode_prepare,location_id)
    if cached:return cached
    try:
        payload=await asyncio.to_thread(photon_request,lat,lon)
        features=payload['features'];props=features[0]['properties'] if features else {}
        parts={k:v.strip() for k in ('country','state','county','city','district','locality','street','housenumber','name') if isinstance(v:=props.get(k),str) and v.strip()}
        address=' · '.join(dict.fromkeys(parts.values()))
    except (urllib.error.URLError,TimeoutError,OSError,KeyError,TypeError,ValueError,IndexError):
        raise HTTPException(503,'地址服务暂时不可用，请稍后重试') from None
    result={'found':bool(address),'address':address,'parts':parts,'provider':'Photon / OpenStreetMap','queried_at':time.time(),'cached':False}
    def save():
        with connect() as con:con.execute('INSERT OR REPLACE INTO geocodes VALUES(?,?,?)',(key,json.dumps(result,ensure_ascii=False),time.time()+(30*86400 if address else 86400)))
    await asyncio.to_thread(save);return result

def install(app):
    @app.get('/api/location-database/status')
    async def status():return await asyncio.to_thread(summary)
    @app.get('/api/location-database/export')
    async def export():
        name=await asyncio.to_thread(snapshot_file)
        stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        return FileResponse(name,media_type='application/vnd.sqlite3',filename='findmy-locations-'+stamp+'.sqlite3',background=BackgroundTask(Path(name).unlink,missing_ok=True))
    @app.get('/api/location-address/{location_id}')
    def address(location_id:int):
        with connect() as con:
            row=con.execute(ADDRESS_SELECT+' WHERE l.id=?',(location_id,)).fetchone()
            if not row:raise HTTPException(404,'位置不存在')
            return address_data(row)


ADDRESS_SELECT="""SELECT l.id,p.status,p.distance_m,p.anchor_key,p.expires_at,g.result_json,
 a.latitude AS anchor_latitude,a.longitude AS anchor_longitude
 FROM locations l LEFT JOIN location_addresses m ON m.location_id=l.id
 LEFT JOIN address_points p ON p.coordinate_key=m.coordinate_key
 LEFT JOIN geocodes g ON g.coordinate_key=p.anchor_key
 LEFT JOIN address_anchors a ON a.coordinate_key=p.anchor_key"""

def address_data(row):
    if row['status']=='ready' and row['expires_at']>time.time() and row['result_json']:
        value=json.loads(row['result_json'])
        value.update(cached=True,distance_m=row['distance_m'],anchor_latitude=row['anchor_latitude'],anchor_longitude=row['anchor_longitude'])
        return {'status':'ready','address':value}
    return {'status':row['status'] if row['status'] in ('retry','empty') else 'pending','address':None}
