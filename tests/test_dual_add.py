import asyncio,base64,csv,io,json,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"services"/"findmy-console"))
import app as host
import dual_add as d
from fastapi.testclient import TestClient
from fastapi import HTTPException

PUB='12'*28
def snapshot():
    accounts=[{'id':i,'label':i,'platform':p,'available':True,'authorized':True,'state':'ready'} for i,p in [('apple1','apple'),('google1','google'),('google2','google')]]
    devices=[{'id':str(i),'sn':str(600000000000+i),'members':[{'id':str(i),'account_id':'google1','platform':'google','pubkey':PUB}]} for i in range(99)]
    return {'accounts':accounts,'devices':devices}
def request():return {'apple_account':'apple1','google_account':'google1','auto_allocate':True,'unburned':False,'rows':[{'sn':'123456789123','pubkey':PUB},{'sn':'123456789124','pubkey':PUB}]}
class PairedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'FINDMY_LOCATION_DB':str(Path(self.tmp.name)/'db')});self.env.start();host.initialize();self.client=TestClient(host.app);self.client.headers['X-Local-Token']=host.TOKEN
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_allocation_duplicates_conflicts(self):
        s=snapshot();r=d.plan(request(),s);self.assertEqual([i['google_account'] for i in r],['google1','google2']);self.assertEqual(r[0]['name'],r[0]['sn'])
        req=request();req['auto_allocate']=False;self.assertIn('容量不足',d.plan(req,s)[1]['error'])
        req['rows'][1]=req['rows'][0];self.assertIn('重复',d.plan(req,s)[1]['error'])
        req=request();req['rows'][0]['sn']=s['devices'][0]['sn'];req['rows'][0]['pubkey']='34'*28;self.assertIn('公钥',d.plan(req,s)[0]['error'])
    def test_durable_start_reservations_and_reuse(self):
        with patch.object(host,'get_snapshot',AsyncMock(return_value=snapshot())):
            r=self.client.post('/api/paired-add/preview',json=request());self.assertEqual(r.status_code,200);j=r.json()
            first=self.client.post('/api/paired-add/'+j['id']+'/start',json={});self.assertEqual(first.status_code,200)
            second=self.client.post('/api/paired-add/'+j['id']+'/start',json={});self.assertEqual(second.json()['id'],j['id'])
            opts=d.options(snapshot());self.assertEqual(next(a for a in opts if a['id']=='google1')['free'],0)
            again=self.client.post('/api/paired-add/preview',json=request()).json();self.assertIn('任务',again['items'][0]['error'])
    def test_capacity_changed_cannot_silently_reallocate(self):
        with patch.object(host,'get_snapshot',AsyncMock(return_value=snapshot())):j=self.client.post('/api/paired-add/preview',json=request()).json()
        s=snapshot();s['devices'].append({'id':'last','sn':'999999999999','members':[{'id':'last','account_id':'google1','platform':'google','pubkey':PUB}]})
        with patch.object(host,'get_snapshot',AsyncMock(return_value=s)):
            self.assertEqual(self.client.post('/api/paired-add/'+j['id']+'/start',json={}).status_code,409)
    def test_csv_and_unicode_names(self):
        raw=('SN,pubkey,名称\n123456789123,'+PUB+',"设备,一"\n').encode('utf-8-sig')
        rows=d.parse_file(d.FileInput(filename='测试.csv',content=base64.b64encode(raw).decode()))['rows'];self.assertEqual(rows[0]['name'],'设备,一')
        with self.assertRaises(HTTPException):d.parse_file(d.FileInput(filename='bad.xls',content=base64.b64encode(b'bad').decode()))
    def test_xlsx_numeric_sn_and_blank_name(self):
        # Minimal in-memory OOXML fixture, not a user workbook.
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w') as z:
            z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
            z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="设备" sheetId="1" r:id="rId1"/></sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>SN</t></is></c><c r="B1" t="inlineStr"><is><t>pubkey</t></is></c></row><row r="2"><c r="A2" t="n"><v>123456789123</v></c><c r="B2" t="inlineStr"><is><t>'+PUB+'</t></is></c></row></sheetData></worksheet>')
        rows=d.parse_file(d.FileInput(filename='设备.xlsx',content=base64.b64encode(out.getvalue()).decode()))['rows']
        self.assertEqual(rows,[{'sn':'123456789123','pubkey':PUB}]);self.assertEqual(d.normalize(rows)[0]['name'],'123456789123')
    def test_partial_success_and_register_resume(self):
        async def scenario():
            row=d.plan(request(),snapshot())[0];job={'id':'test','state':'running','request':request(),'items':[row]}
            device={'id':'stable','sn':row['sn'],'name':row['name'],'pubkey':PUB,'registration':'unregistered'};registered=False;calls=[]
            async def fake(a,path,body=None):
                calls.append(path)
                if path=='keys/import-sn':raise HTTPException(404,'未查到密钥')
                if path=='status':return {'devices':[dict(device,registration='registered' if registered else 'unregistered')],'jobs':[]}
                if path.startswith('jobs/register'):return {'id':'job1'}
                return {}
            with patch.object(d,'call',fake),patch.object(d,'save'):
                await d.process(job,row);self.assertEqual(row['apple'],'error');self.assertEqual(row['google'],'registering')
                registered=True;await d.process(job,row);self.assertEqual(row['google'],'done');self.assertEqual(calls.count('keys/import-sn'),1);self.assertEqual(sum(p.startswith('jobs/register') for p in calls),1);self.assertNotIn('devices',calls)
        asyncio.run(scenario())
    def test_apple_conflict_blocks_google(self):
        async def scenario():
            row=d.plan(request(),snapshot())[0];job={'id':'test','request':request(),'items':[row]}
            call=AsyncMock(side_effect=HTTPException(409,'公钥不一致'))
            with patch.object(d,'call',call),patch.object(d,'save'):await d.process(job,row)
            self.assertEqual(call.await_count,1);self.assertEqual(row['google'],'error')
        asyncio.run(scenario())
    def test_retry_does_not_overwrite_google_success(self):
        async def scenario():
            row=d.plan(request(),snapshot())[0];row['google']='done';job={'id':'test','request':request(),'items':[row]}
            with patch.object(d,'call',AsyncMock(side_effect=HTTPException(409,'公钥不一致'))) as call,patch.object(d,'save'):
                await d.process(job,row)
                self.assertEqual(call.await_count,1);self.assertEqual(row['google'],'done');self.assertEqual(row['apple'],'error')
        asyncio.run(scenario())
if __name__=='__main__':unittest.main()
