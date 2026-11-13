"""On-demand OSM/Photon reverse lookup; separate from Google location collection."""
import asyncio
import hashlib
import math
import os
import time

import httpx


class GeocodeError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


class ReverseGeocoder:
    def __init__(self, store):
        self.store = store
        self.endpoint = os.environ.get('GOOGLEFINDMY_PHOTON_URL', 'https://photon.komoot.io').rstrip('/')
        self.lock = asyncio.Lock()

    def key(self, lat, lon):
        # Cache keys don't expose coordinates in the otherwise encrypted database.
        return hashlib.sha256(f'{self.endpoint}|{lat:.7f},{lon:.7f}'.encode()).hexdigest()

    def cached(self, lat, lon, cache=None):
        if cache is None:
            cache = self.store.get('reverse:cache', {})
        entry = cache.get(self.key(lat, lon))
        if entry and entry['expires_at'] > time.time():
            return {**entry['result'], 'cached': True}
        return None

    def with_cached_addresses(self, rows):
        cache = self.store.get('reverse:cache', {})
        result = []
        for point in rows:
            lat, lon = point.get('latitude'), point.get('longitude')
            address = self.cached(lat, lon, cache) if self.valid(lat, lon) else None
            result.append({**point, 'reverse_address': address} if address else point)
        return result

    @staticmethod
    def valid(lat, lon):
        return (isinstance(lat, (int, float)) and not isinstance(lat, bool)
                and isinstance(lon, (int, float)) and not isinstance(lon, bool)
                and math.isfinite(lat) and math.isfinite(lon)
                and -90 <= lat <= 90 and -180 <= lon <= 180)

    async def lookup(self, lat, lon):
        if not self.valid(lat, lon):
            raise GeocodeError('经纬度无效', 422)
        cached = self.cached(lat, lon)
        if cached:
            return cached
        if self.lock.locked():
            raise GeocodeError('正在查询地址，请稍后再试', 429)
        async with self.lock:
            now = time.time()
            rate = self.store.get('reverse:rate', {})
            if now < rate.get('next_at', 0):
                raise GeocodeError('地址查询较频繁，请稍后再试', 429)
            if now - rate.get('hour_start', 0) >= 3600:
                rate = {'hour_start': now, 'count': 0}
            if rate.get('count', 0) >= 120:
                raise GeocodeError('本小时地址查询已达上限，请稍后再试；已有缓存仍可查看', 429)
            rate.update(count=rate.get('count', 0)+1, next_at=now+2)
            self.store.put('reverse:rate', rate)
            try:
                # Google credentials/proxy are never sent to the address service.
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    response = await client.get(self.endpoint+'/reverse',
                        params={'lat': lat, 'lon': lon, 'limit': 1, 'radius': 1},
                        headers={'User-Agent': 'FindHubWorkbench/1.0 (manual reverse geocoding)',
                                 'Accept': 'application/json'})
                if response.status_code in (403, 429):
                    rate['next_at'] = time.time()+60
                    self.store.put('reverse:rate', rate)
                    raise GeocodeError('地址服务暂时限制请求，请稍后重试', 429)
                response.raise_for_status()
                result = self.parse(response.json())
            except GeocodeError:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
                raise GeocodeError('地址服务暂时不可用，请稍后重试') from None
            result.update(provider='Photon / OpenStreetMap', queried_at=time.time(), cached=False)
            cache = self.store.get('reverse:cache', {})
            cache = {k:v for k,v in cache.items() if v['expires_at'] > time.time()}
            while len(cache) >= 5000:
                del cache[min(cache, key=lambda k:cache[k]['expires_at'])]
            cache[self.key(lat, lon)] = {'result': result,
                'expires_at': time.time()+(30*86400 if result['found'] else 86400)}
            self.store.put('reverse:cache', cache)
            return result

    @staticmethod
    def parse(payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('features'), list):
            raise ValueError('Invalid GeoJSON')
        if not payload['features']:
            return {'found': False, 'address': '', 'parts': {}}
        properties = payload['features'][0]['properties']
        if not isinstance(properties, dict):
            raise ValueError('Invalid properties')
        parts = {key:value.strip() for key in
                 ('country','state','county','city','district','locality','street','housenumber','name')
                 if isinstance(value := properties.get(key), str) and value.strip()}
        address = ' · '.join(dict.fromkeys(parts.values()))
        return {'found': bool(address), 'address': address, 'parts': parts}
