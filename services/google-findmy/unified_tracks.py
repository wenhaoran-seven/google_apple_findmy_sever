"""Read-only, exact-SN history federation. Never issues cloud location requests."""
import asyncio
import json
import math
import re
import urllib.request
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from central_locations import read_track, install as install_database

PEERS = {'apple': 8765, 'google1': 8767, 'google2': 8769, 'google3': 8771}
LABELS = {'apple': 'Apple', 'google1': 'Google 账号1', 'google2': 'Google 账号2', 'google3': 'Google 账号3'}

def normalized(point, source, device_id):
    try:
        value = point['timestamp']
        stamp = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        lat, lon = float(point['latitude']), float(point['longitude'])
        if not all(math.isfinite(v) for v in (stamp, lat, lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        result = {k: point.get(k) for k in ('accuracy_m', 'altitude', 'status', 'is_own_report', 'unknown_11', 'unknown_11_received_at')}
        result.update(timestamp=stamp, latitude=lat, longitude=lon, source=source,
                      platform='apple' if source == 'apple' else 'google', source_label=LABELS[source], device_id=device_id)
        return result
    except (KeyError, ValueError, TypeError, OverflowError):
        return None

def snapshot(service, source, sn='', device_id=''):
    devices = service.devices.items() if source == 'apple' else ((d['id'], d) for d in service.store.all('device:'))
    rows, matches = [], []
    for ident, device in devices:
        if (str(device.get('sn') or '') != sn) if sn else (ident != device_id):
            continue
        matches.append({'id': ident, 'name': device.get('name', ''), 'source': source})
        points = service.location_cache.get(ident, {}).get('reports', []) if source == 'apple' else service.store.get('history:'+ident, [])
        rows.extend(row for p in points if (row := normalized(p, source, ident)) is not None)
    return {'reports': rows, 'matches': matches, 'source': source}

def peer_snapshot(source, sn):
    # Hard-coded loopback peers only: no user-controlled URLs or proxy egress.
    origin = f'http://127.0.0.1:{PEERS[source]}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(origin+'/', timeout=5) as response:
        page = response.read(500_000).decode('utf-8')
    match = re.search(r'name="local-token" content="([^"]+)"', page)
    if not match:
        raise ValueError('Missing peer token')
    req = urllib.request.Request(origin+'/api/track-source?sn='+sn, headers={'X-Local-Token': match[1]})
    with opener.open(req, timeout=12) as response:
        return json.load(response)

def combine(results):
    reports, matches, warnings, available = [], [], [], []
    seen = set()
    for source, result in results:
        if isinstance(result, BaseException):
            warnings.append(LABELS[source]+'历史位置暂时不可用，当前结果不完整。')
            continue
        available.append(source)
        matches.extend(result.get('matches', []))
        for row in result.get('reports', []):
            # Keep equal-time Apple/Google observations distinct; remove repeated
            # records within one source/device without dropping different coordinates.
            key = json.dumps(row, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key); reports.append(row)
    reports.sort(key=lambda p: p['timestamp'])
    return {'reports': reports, 'matches': matches, 'warnings': warnings, 'available_sources': available}

def install(app, source):
    install_database(app)
    @app.get('/api/track-source')
    def track_source(request: Request, sn: str = '', device_id: str = ''):
        if not re.fullmatch(r'[0-9]{12}', sn) and not (not sn and device_id):
            raise HTTPException(422, '请提供有效 SN 或设备 ID')
        return snapshot(request.app.state.service, source, sn, device_id)

    @app.get('/api/unified-track')
    async def unified_track(request: Request, sn: str = '', device_id: str = ''):
        if not re.fullmatch(r'[0-9]{12}', sn) and not (not sn and device_id):
            raise HTTPException(422, '请提供有效 SN 或设备 ID')
        return await asyncio.to_thread(read_track, source, sn, device_id)
