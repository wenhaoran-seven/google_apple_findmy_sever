"""Server-only Baidu credentials; canonical inputs and cache keys stay WGS84."""
import json,os,time,urllib.request,urllib.parse,logging
from pathlib import Path
from datetime import datetime,timezone,timedelta

blocked_until=0
failures=0
class ProviderError(Exception):pass
def enabled():return bool(os.environ.get('BAIDU_MAP_AK_FILE'))
def available():return enabled() and time.time()>=blocked_until
def baidu(lat,lon):
    global blocked_until,failures
    if not available():raise ProviderError('Baidu cooldown')
    key=Path(os.environ['BAIDU_MAP_AK_FILE']).read_text().strip()
    params={'ak':key,'output':'json','coordtype':'wgs84ll','location':f'{lat:.7f},{lon:.7f}','extensions_town':'true'}
    status=None
    try:
        req=urllib.request.Request('https://api.map.baidu.com/reverse_geocoding/v3/?'+urllib.parse.urlencode(params),headers={'Accept':'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=18) as response:data=json.load(response)
        status=data.get('status') if isinstance(data.get('status'),int) else None
        if data.get('status')!=0:raise ProviderError('Baidu status '+str(data.get('status')))
        raw=data.get('result') or {};address=str(raw.get('formatted_address') or '').strip()
        if not address:raise ProviderError('Baidu empty result')
    except Exception:
        failures+=1
        now=time.time()
        if status==302:
            # Daily quota: retry after the next Beijing midnight, not every few minutes.
            current=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
            reset=(current+timedelta(days=1)).replace(hour=0,minute=5,second=0,microsecond=0)
            blocked_until=reset.timestamp();delay=int(blocked_until-now)
        else:
            delay=min(3600,300*2**min(failures-1,4));blocked_until=now+delay
        logging.warning('Baidu request unavailable: status=%s; cooldown=%ss',status,delay)
        # Do not leak the token-bearing request URL through exceptions/logs.
        raise ProviderError('Baidu unavailable; cooldown '+str(delay)+'s') from None
    failures=0
    comp=raw.get('addressComponent') or {}
    return {'found':True,'address':address,'parts':comp,'description':raw.get('sematic_description') or '',
            'provider':'Baidu','queried_at':time.time(),'cached':False,'input_coordtype':'wgs84ll'}
def reverse(lat,lon,photon_request,allow_fallback=True):
    if available():
        try:return baidu(lat,lon)
        except ProviderError:
            if not allow_fallback:raise
    if not allow_fallback:raise ProviderError('Baidu unavailable')
    payload=photon_request(lat,lon);features=payload['features'];props=features[0]['properties'] if features else {}
    parts={k:v.strip() for k in ('country','state','county','city','district','locality','street','housenumber','name') if isinstance(v:=props.get(k),str) and v.strip()}
    address=' · '.join(dict.fromkeys(parts.values()))
    return {'found':bool(address),'address':address,'parts':parts,'provider':'Photon / OpenStreetMap','queried_at':time.time(),'cached':False}
