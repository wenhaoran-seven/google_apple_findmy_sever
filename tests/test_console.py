import os,sys,tempfile,unittest,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"services"/"findmy-console"))
import app as c
from fastapi.testclient import TestClient
from unittest.mock import patch

class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'FINDMY_LOCATION_DB':str(Path(self.tmp.name)/'db.sqlite3')});self.env.start();c.initialize();c.cached=None;c.cached_at=0
        self.client=TestClient(c.app);self.client.headers['X-Local-Token']=c.TOKEN
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_relay_read_retry_and_write_no_retry(self):
        from unittest.mock import MagicMock
        c.relay_tokens.clear()
        a={'id':'apple1','port':8765,'platform':'apple'}
        html=MagicMock();html.__enter__.return_value.read.return_value=b'<meta name="local-token" content="test-token">'
        payload=MagicMock();payload.__enter__.return_value.read.return_value=b'{"location_ready":true}'
        op=MagicMock();op.open.side_effect=[TimeoutError(),html,payload]
        with patch.object(c.urllib.request,'build_opener',return_value=op),patch.object(c.time,'sleep'):
            self.assertTrue(c.relay(a,'status')['location_ready'])
        self.assertEqual(op.open.call_count,3)
        c.relay_tokens.clear();op=MagicMock();op.open.side_effect=[html,TimeoutError()]
        with patch.object(c.urllib.request,'build_opener',return_value=op):
            with self.assertRaises(c.HTTPException) as error:c.relay(a,'login',{'account':'test'})
        self.assertEqual(op.open.call_count,2)
        self.assertIn('操作结果尚未确认',error.exception.detail)
        c.relay_tokens.clear()

    def test_shared_device_rename(self):
        rows={a['id']:[] for a in c.accounts()}
        for ident in ('apple1','google2'):rows[ident]=[{'id':ident+'-device','sn':'123456789012','name':'old'}]
        rows['google1']=[{'id':'other','sn':'999999999999','name':'untouched'}]
        def collect(a):return {**a,'available':True,'devices':rows[a['id']]}
        def write(a,path,body):
            for d in rows[a['id']]:
                if path=='devices/'+d['id']+'/rename':d['name']=body['name']
        body={'device_key':'sn:123456789012','name':' shared '}
        with patch.object(c,'collector',collect),patch.object(c,'relay',side_effect=write) as relay:
            r=self.client.post('/api/devices/rename',json=body)
            self.assertEqual(r.status_code,200);self.assertEqual(r.json()['updated'],2)
            self.assertEqual(relay.call_count,2)
            self.assertEqual(rows['google1'][0]['name'],'untouched')
            self.assertEqual(rows['apple1'][0]['name'],'shared')
            self.client.post('/api/devices/rename',json=body)
            self.assertEqual(relay.call_count,2)
        rows['apple1'][0]['name']='old'
        with patch.object(c,'collector',collect),patch.object(c,'relay',side_effect=TimeoutError):
            r=self.client.post('/api/devices/rename',json=body)
            self.assertEqual(r.status_code,409);self.assertIn('尚未全部同步',r.json()['detail'])
        with patch.object(c,'collector',lambda a:{**collect(a),'available':False}),patch.object(c,'relay') as relay:
            self.assertEqual(self.client.post('/api/devices/rename',json=body).status_code,409)
            relay.assert_not_called()
        self.assertEqual(self.client.post('/api/devices/rename',json={**body,'name':'   '}).status_code,422)

    def test_apple_battery_codes(self):
        from unittest.mock import MagicMock
        con=MagicMock()
        for code in range(16):
            con.execute.return_value.fetchone.return_value={'status':0x20|code,'timestamp':123}
            b=c.apple_battery(con,'123456789012','apple','test')
            self.assertEqual(b['percent'],100-code*10 if code<10 else 5)
            self.assertEqual(b['critical'],code>=11)
        for value in (None,-1,256,'bad'):
            con.execute.return_value.fetchone.return_value={'status':value,'timestamp':123}
            self.assertIsNone(c.apple_battery(con,'123456789012','apple','test'))

    def test_rename_account(self):
        before=c.account('google1')
        r=self.client.post('/api/accounts/google1/rename',json={'label':'  新名称  '})
        self.assertEqual(r.status_code,200)
        after=c.account('google1')
        self.assertEqual(after['label'],'新名称')
        for key in ('id','source','port','platform'):self.assertEqual(before[key],after[key])
        c.initialize();self.assertEqual(c.account('google1')['label'],'新名称')
        self.assertEqual(self.client.post('/api/accounts/apple1/rename',json={'label':'苹果测试'}).status_code,200)
        self.assertEqual(self.client.post('/api/accounts/google1/rename',json={'label':'   '}).status_code,422)
        self.assertEqual(self.client.post('/api/accounts/no-such/rename',json={'label':'test'}).status_code,404)

    def test_baseline_persists_success(self):
        fixture=Path(self.tmp.name)/'baseline';fixture.mkdir()
        (fixture/'burn-baseline.json').write_text(json.dumps({'all_sns':['900000000001','900000000002'],'unburned_sns':['900000000002']}))
        with patch.object(c,'ROOT',fixture):c.initialize()
        with c.db.connect() as con:
            counts=dict(con.execute('SELECT state,COUNT(*) FROM burn_registry GROUP BY state'))
            self.assertEqual(counts,{'burned':1,'unburned':1})
            sn=con.execute("SELECT sn FROM burn_registry WHERE state='unburned'").fetchone()[0]
        body={'sn':sn,'session':'a'*16}
        self.assertEqual(self.client.post('/api/burn/claim',json=body).status_code,200)
        self.assertEqual(self.client.post('/api/burn/claim',json={**body,'session':'b'*16}).status_code,409)
        self.assertEqual(self.client.post('/api/burn/complete',json={**body,'ack':'OK+SETKEY=sn:123456789123'}).status_code,409)
        self.assertEqual(self.client.post('/api/burn/complete',json={**body,'ack':'OK+SETKEY=sn:'+sn}).status_code,200)
        c.initialize()
        with c.db.connect() as con:self.assertEqual(con.execute('SELECT state FROM burn_registry WHERE sn=?',(sn,)).fetchone()[0],'burned')
    def test_account_isolation_and_input(self):
        for platform,ident in [('apple','apple2'),('google','google4')]:
            r=self.client.post('/api/accounts',json={'platform':platform,'label':'测试账号'});self.assertEqual(r.status_code,200);self.assertEqual(r.json()['id'],ident)
        rows=c.accounts();self.assertEqual(len({a['port'] for a in rows}),6);self.assertEqual(len({a['source'] for a in rows}),6)
        self.assertEqual(self.client.post('/api/accounts',json={'platform':'google','label':' '}).status_code,422)
    def test_guard_and_allowlist(self):
        self.assertEqual(self.client.get('/api/console',headers={'X-Local-Token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/api/accounts',headers={'Origin':'https://evil.example'},json={}).status_code,403)
        self.assertEqual(self.client.get('/api/accounts/google1/proxy/../../secrets').status_code,404)
        self.assertEqual(self.client.post('/api/accounts/google1/proxy/auth/import/extra',json={}).status_code,404)
    def test_latest_metadata_and_sn_merge(self):
        sn='123456789123';now=time.time()
        c.db.persist # ensure module loaded
        with c.db.connect() as con:
            for source,stamp,status,alt in [('apple',now-20,0,80),('google1',now,3,70)]:
                p={'timestamp':stamp,'latitude':22,'longitude':113,'status':status,'altitude':alt,'accuracy_m':12}
                con.execute('INSERT INTO locations(source,platform,sn,device_id,device_name,timestamp,latitude,longitude,fingerprint,raw_json,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(source,'apple' if source=='apple' else 'google',sn,source,'x',stamp,22,113,source,json.dumps(p),now,now))
        def collector(a):return {**a,'available':True,'devices':[{'id':a['id'],'sn':sn,'name':'设备','pubkey':'01'*28}]} if a['id'] in ('apple1','google1') else {**a,'available':True,'devices':[]}
        with patch.object(c,'collector',collector):result=c.snapshot()
        self.assertEqual(len(result['devices']),1);d=result['devices'][0];self.assertEqual(len(d['members']),2)
        self.assertEqual(d['latest']['status'],3);self.assertEqual(d['latest']['altitude'],70);self.assertEqual(d['latest']['platform'],'google');self.assertEqual(d['latest']['address_status'],'pending')
    def test_import_dedup(self):
        body={'account_id':'apple1','sns':'123456789123,123456789123\n123456789124','enabled':True}
        a=self.client.post('/api/import-sn',json=body).json();b=self.client.post('/api/import-sn',json=body).json();self.assertEqual(a,b)
        self.assertEqual(self.client.get('/api/import-sn/'+a['id']).json()['total'],2)

if __name__=='__main__':unittest.main()
