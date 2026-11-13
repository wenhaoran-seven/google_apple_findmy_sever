"""One independent worker; durable queue and cross-platform 100m anchor cache."""
import json
import os
import math
import time
import logging
import central_locations as db
import address_provider as provider

SCHEMA = '''
CREATE TABLE IF NOT EXISTS baidu_upgrades(coordinate_key TEXT PRIMARY KEY,next_at REAL DEFAULT 0,attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS address_provider_rate(provider TEXT PRIMARY KEY,next_at REAL,hour_start REAL,count INTEGER);

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
'''

def initialize():
    db.initialize()
    with db.connect() as con:
        con.executescript(SCHEMA)
        high=con.execute('SELECT COALESCE(MAX(id),0) FROM locations').fetchone()[0]
        for key,value in [('address_initial_end',high),('address_live_cursor',high),('address_history_cursor',0)]:
            con.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',(key,str(value)))
        # Migrate only actual successful provider lookups, never derived points.
        for row in con.execute('SELECT * FROM geocodes'):
            data=json.loads(row['result_json'])
            if data.get('found'):
                if provider.enabled() and data.get('provider')!='Baidu':con.execute('INSERT OR IGNORE INTO baidu_upgrades(coordinate_key) VALUES(?)',(row['coordinate_key'],))
                lat,lon=map(float,row['coordinate_key'].split(','))
                con.execute('INSERT OR IGNORE INTO address_anchors VALUES(?,?,?)',(row['coordinate_key'],lat,lon))

def discover(limit=1000):
    with db.connect() as con:
        for kind in ['live','history']:
            key='address_'+kind+'_cursor'
            cursor=int(con.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()[0])
            end=int(con.execute("SELECT value FROM metadata WHERE key='address_initial_end'").fetchone()[0]) if kind=='history' else 9223372036854775807
            rows=con.execute('SELECT id,latitude,longitude FROM locations WHERE id>? AND id<=? ORDER BY id LIMIT ?',(cursor,end,limit)).fetchall()
            for row in rows:
                coordinate=db.coordinate_key(row['latitude'],row['longitude'])
                con.execute('INSERT INTO address_points(coordinate_key,latitude,longitude,location_id,priority) VALUES(?,?,?,?,?) ON CONFLICT(coordinate_key) DO UPDATE SET priority=MAX(priority,excluded.priority)',(coordinate,row['latitude'],row['longitude'],row['id'],int(kind=='live')))
                con.execute('INSERT OR IGNORE INTO location_addresses VALUES(?,?)',(row['id'],coordinate))
            if rows:con.execute('UPDATE metadata SET value=? WHERE key=?',(str(rows[-1]['id']),key))

def distance(lat,lon,other_lat,other_lon):
    a,b=math.radians(lat),math.radians(other_lat)
    h=math.sin((b-a)/2)**2+math.cos(a)*math.cos(b)*math.sin(math.radians(other_lon-lon)/2)**2
    return 6371008.8*2*math.asin(min(1,math.sqrt(max(0,h))))

def nearest(con,lat,lon,now):
    delta=math.degrees(100/6371008.8)
    # Latitude index bounds candidates; haversine handles poles and dateline.
    rows=con.execute('SELECT a.*,g.expires_at FROM address_anchors a JOIN geocodes g USING(coordinate_key) WHERE a.latitude BETWEEN ? AND ? AND g.expires_at>?',(lat-delta,lat+delta,now))
    best=None
    for row in rows:
        meters=distance(lat,lon,row['latitude'],row['longitude'])
        if meters<=100 and (best is None or meters<best[1]):best=(row,meters)
    return best

def attach(con,key,anchor,meters):
    con.execute("UPDATE address_points SET status='ready',anchor_key=?,distance_m=?,expires_at=?,attempts=0,next_at=0 WHERE coordinate_key=?",(anchor['coordinate_key'],meters,anchor['expires_at'],key))

def reserve(con,now):
    if provider.available():
        rate=con.execute("SELECT * FROM address_provider_rate WHERE provider='baidu'").fetchone()
        start,count=(rate['hour_start'],rate['count']) if rate and now-rate['hour_start']<3600 else (now,0)
        limit=max(1,int(os.environ.get('BAIDU_REQUESTS_PER_HOUR','3600')))
        interval=max(1,float(os.environ.get('BAIDU_REQUEST_INTERVAL','1')))
        if count>=limit:return max(now+interval,start+3600)
        if rate and now<rate['next_at']:return rate['next_at']
        con.execute("INSERT OR REPLACE INTO address_provider_rate VALUES('baidu',?,?,?)",(now+interval,start,count+1));return None
    rate=con.execute('SELECT * FROM geocode_rate WHERE id=1').fetchone()
    start,count=(rate['hour_start'],rate['count']) if rate and now-rate['hour_start']<3600 else (now,0)
    if count>=120:return max(now+2,start+3600)
    if rate and now<rate['next_at']:return rate['next_at']
    con.execute('INSERT OR REPLACE INTO geocode_rate VALUES(1,?,?,?)',(now+2,start,count+1))
    return None

def tick(batch=100):
    """Bounded cache work plus at most one external request; no network under DB lock."""
    discover()
    now=time.time();request=None;hits=0
    with db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE address_points SET status='pending',anchor_key=NULL,next_at=0 WHERE coordinate_key IN (SELECT coordinate_key FROM address_points WHERE status='ready' AND expires_at<=? LIMIT 500)",(now,))
        # Inspect postponed work too: newly available anchors may resolve it for free.
        cursor_row=con.execute("SELECT value FROM metadata WHERE key='address_sweep_cursor'").fetchone()
        cursor=int(cursor_row[0]) if cursor_row else 0
        sweep=con.execute("SELECT rowid AS rid,* FROM address_points WHERE status!='ready' AND rowid>? ORDER BY rowid LIMIT ?",(cursor,batch)).fetchall()
        con.execute("INSERT OR REPLACE INTO metadata VALUES('address_sweep_cursor',?)",(str(sweep[-1]['rid'] if sweep else 0),))
        due=con.execute("SELECT rowid AS rid,* FROM address_points WHERE status!='ready' AND next_at<=? ORDER BY priority DESC,next_at,rowid LIMIT ?",(now,batch)).fetchall()
        candidates={p['coordinate_key']:p for p in due+sweep}
        for point in candidates.values():
            found=nearest(con,point['latitude'],point['longitude'],now)
            if found:attach(con,point['coordinate_key'],*found);hits+=1
        # Every fifth dispatch prefers historical points, preventing starvation.
        counter_row=con.execute("SELECT value FROM metadata WHERE key='address_dispatch_count'").fetchone()
        count=int(counter_row[0]) if counter_row else 0
        direction='ASC' if count%5==4 else 'DESC'
        request=con.execute("SELECT * FROM address_points WHERE status!='ready' AND next_at<=? ORDER BY priority "+direction+',next_at,rowid LIMIT 1',(now,)).fetchone()
        if provider.available() and (count%5==0 or request is None):
            upgrade=con.execute('''SELECT a.coordinate_key,a.latitude,a.longitude,u.attempts,1 AS upgrade
                FROM baidu_upgrades u JOIN address_anchors a USING(coordinate_key)
                LEFT JOIN address_points p ON p.anchor_key=a.coordinate_key
                LEFT JOIN locations l ON l.id=p.location_id WHERE u.next_at<=?
                GROUP BY a.coordinate_key ORDER BY MAX(l.timestamp) DESC LIMIT 1''',(now,)).fetchone()
            if upgrade:request=upgrade
        if request:
            request=dict(request)
            request['baidu_reserved']=provider.available()
            wait=reserve(con,now)
            if wait:request=None
            else:
                con.execute("INSERT OR REPLACE INTO metadata VALUES('address_dispatch_count',?)",(str(count+1),))
                if request.get('upgrade'):con.execute('UPDATE baidu_upgrades SET next_at=? WHERE coordinate_key=?',(now+60,request['coordinate_key']))
                else:con.execute("UPDATE address_points SET status='pending',next_at=? WHERE coordinate_key=?",(now+60,request['coordinate_key']))
    if request:
        try:
            def fallback(lat,lon):
                if request.get('baidu_reserved'):
                    with db.connect() as con:
                        con.execute('BEGIN IMMEDIATE')
                        if reserve(con,time.time()):raise provider.ProviderError('Fallback quota reached')
                return db.photon_request(lat,lon)
            result=provider.reverse(request['latitude'],request['longitude'],fallback,allow_fallback=not request.get('upgrade'));address=result['address']
            with db.connect() as con:
                if address:
                    expiry=time.time()+30*86400
                    con.execute('INSERT OR REPLACE INTO geocodes VALUES(?,?,?)',(request['coordinate_key'],json.dumps(result,ensure_ascii=False),expiry))
                    con.execute('INSERT OR REPLACE INTO address_anchors VALUES(?,?,?)',(request['coordinate_key'],request['latitude'],request['longitude']))
                    if not request.get('upgrade'):attach(con,request['coordinate_key'],{'coordinate_key':request['coordinate_key'],'expires_at':expiry},0)
                    if result['provider']=='Baidu':con.execute('DELETE FROM baidu_upgrades WHERE coordinate_key=?',(request['coordinate_key'],))
                    elif provider.enabled():con.execute('INSERT OR IGNORE INTO baidu_upgrades(coordinate_key) VALUES(?)',(request['coordinate_key'],))
                else:
                    con.execute("UPDATE address_points SET status='empty',attempts=attempts+1,next_at=? WHERE coordinate_key=?",(time.time()+86400,request['coordinate_key']))
        except Exception as error:
            attempts=request['attempts']+1
            with db.connect() as con:
                if request.get('upgrade'):con.execute('UPDATE baidu_upgrades SET attempts=?,next_at=? WHERE coordinate_key=?',(attempts,time.time()+min(3600,30*2**min(attempts,7)),request['coordinate_key']))
                else:con.execute("UPDATE address_points SET status='retry',attempts=?,next_at=? WHERE coordinate_key=?",(attempts,time.time()+min(3600,30*2**min(attempts,7)),request['coordinate_key']))
            logging.warning('Address provider request failed: %s',type(error).__name__)
    return {'cache_hits':hits,'provider_request':bool(request)}

def main():
    import fcntl
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(message)s')
    lock=open(db.database_path().parent/'address-worker.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    initialize();logging.info('Automatic address worker started')
    last=0
    while True:
        started=time.monotonic()
        try:
            result=tick()
            if time.time()-last>60:
                logging.info('Address worker healthy; last batch %s',result);last=time.time()
        except Exception:
            logging.exception('Address worker batch failed; retrying')
        interval=max(1,float(os.environ.get('BAIDU_REQUEST_INTERVAL','1'))) if provider.available() else 2
        time.sleep(max(0.1,interval-(time.monotonic()-started)))

if __name__=='__main__':main()
