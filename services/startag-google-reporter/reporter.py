"""Read-only Google history exporter. Persistent encrypted outbox; no Google API calls."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENDPOINT = 'https://app.startag.cc/api/device/location/report'
SOURCES = ['/opt/google-findmy/data', '/opt/google-findmy-2/data', '/opt/google-findmy-3/data']
TZ = timezone(timedelta(hours=8))

def valid(p):
    for k in ('latitude', 'longitude', 'timestamp'):
        v = p.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return False
    return -90 <= p['latitude'] <= 90 and -180 <= p['longitude'] <= 180 and 0 < p['timestamp'] < 253402185600

def canonical(points):
    grouped = {}
    def quality(p):
        a = p.get('accuracy_m')
        return (a if isinstance(a, (int, float)) and math.isfinite(a) and a >= 0 else float('inf'),
                p['latitude'], p['longitude'])
    for p in points:
        if valid(p):
            sec = int(p['timestamp'])
            if sec not in grouped or quality(p) < quality(grouped[sec]):
                grouped[sec] = p
    return grouped

def payload(d, p, email):
    when = datetime.fromtimestamp(p['timestamp'], TZ).strftime('%Y-%m-%d %H:%M:%S')
    return dict(sn=d['sn'], lat=p['latitude'], lng=p['longitude'],
                deviceAdvertisementTime=when, findmyLocationTime=when,
                reportAccount=email, reportAccountName=email, reportPlatform='google',
                reportDeviceInfo={'deviceName': d.get('name', ''), 'locationTimestamp': p['timestamp'],
                                  'coordinateSystem': 'WGS84', 'advertisementTimeSource': 'location_time'})

def read_source(directory):
    directory = Path(directory)
    cipher = AESGCM((directory / '.vault-key').read_bytes())
    def decode(name, blob):
        return json.loads(cipher.decrypt(blob[:12], blob[12:], name.encode()))
    with sqlite3.connect((directory / 'state.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=20) as db:
        db.execute('BEGIN')
        name = 'auth:username'
        row = db.execute('SELECT value FROM kv WHERE name=?', (name,)).fetchone()
        email = decode(name, row[0]) if row else None
        if not isinstance(email, str) or '@' not in email:
            raise ValueError('source account email unavailable')
        result = []
        for name, blob in db.execute("SELECT name,value FROM kv WHERE name LIKE 'device:%'").fetchall():
            d = decode(name, blob)
            if d.get('registration') != 'registered' or d.get('delete_state'):
                continue
            key = 'history:' + d['id']
            row = db.execute('SELECT value FROM kv WHERE name=?', (key,)).fetchone()
            result.append((d, decode(key, row[0]) if row else []))
    return email, result

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('destination redirect refused')

def send(p):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    req = urllib.request.Request(ENDPOINT, data=json.dumps(p, allow_nan=False).encode(),
                                 headers={'Content-Type': 'application/json', 'User-Agent': 'StartagGoogleReporter/1.0'})
    with opener.open(req, timeout=35) as resp:
        if not 200 <= resp.status < 300:
            raise ValueError('destination HTTP failure')
        result = json.load(resp)
    if result.get('code') != 200:
        raise ValueError('destination business failure code=' + str(result.get('code')))
    data = result.get('data', {})
    for remote, local in [('tag_device_sn', 'sn'), ('report_platform', 'reportPlatform'),
                          ('report_account_name', 'reportAccountName'), ('report_account', 'reportAccount'),
                          ('device_advertisement_time', 'deviceAdvertisementTime'), ('findmy_location_time', 'findmyLocationTime')]:
        if data.get(remote) != p[local]:
            raise ValueError('destination echo mismatch: ' + remote)
    if data.get('storage_target') not in ('tracking', 'unbound'):
        raise ValueError('destination storage target invalid')
    return {k: data.get(k) for k in ('storage_target', 'tracking_record_id', 'pending_report_id')}

class Reporter:
    def __init__(self, state):
        state = Path(state)
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = state / '.outbox-key'
        if not key.exists():
            if (state / 'state.sqlite3').exists():
                raise RuntimeError('outbox key missing')
            with key.open('xb') as f:
                f.write(os.urandom(32))
            key.chmod(0o600)
        self.cipher = AESGCM(key.read_bytes())
        self.db = sqlite3.connect(state / 'state.sqlite3', timeout=20)
        (state / 'state.sqlite3').chmod(0o600)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS cursors (identity TEXT PRIMARY KEY, latest INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, account INTEGER, sn TEXT, second INTEGER,
          kind TEXT, body BLOB, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0,
          retry_at REAL DEFAULT 0, result TEXT, error TEXT, sent_at REAL);
        CREATE TABLE IF NOT EXISTS scans (account INTEGER PRIMARY KEY, checked REAL, summary TEXT);
        ''')

    def discover(self, account, email, devices):
        stats = dict(registered=len(devices), with_positions=0, source_points=0, valid_points=0, same_second_collapsed=0, queued=0)
        with self.db:
            for d, points in devices:
                sn = d.get('sn')
                if not isinstance(sn, str) or len(sn) != 12 or not sn.isascii() or not sn.isdecimal():
                    raise ValueError('invalid source SN')
                groups = canonical(points)
                stats['source_points'] += len(points)
                stats['valid_points'] += sum(valid(p) for p in points)
                stats['same_second_collapsed'] += sum(valid(p) for p in points) - len(groups)
                stats['with_positions'] += bool(groups)
                ident = hashlib.sha256(f'{account}|{email}|{d["id"]}|{sn}'.encode()).hexdigest()
                row = self.db.execute('SELECT latest FROM cursors WHERE identity=?', (ident,)).fetchone()
                previous = row[0] if row else -1
                latest = max(groups, default=previous)
                selected = sorted(groups) if row is None else ([latest] if latest > previous else [])
                for sec in selected:
                    item_id = hashlib.sha256(f'{sn}|{sec}'.encode()).hexdigest()
                    p = payload(d, groups[sec], email)
                    nonce = os.urandom(12)
                    body = nonce + self.cipher.encrypt(nonce, json.dumps(p, ensure_ascii=False).encode(), item_id.encode())
                    count = self.db.execute('INSERT OR IGNORE INTO outbox(id,account,sn,second,kind,body) VALUES(?,?,?,?,?,?)',
                                            (item_id, account, sn, sec, 'history' if row is None else 'live', body)).rowcount
                    stats['queued'] += count
                self.db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?)', (ident, max(previous, latest)))
            self.db.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)', (account, time.time(), json.dumps(stats)))
        return stats

    def flush_one(self, sender=send):
        row = self.db.execute("SELECT id,body,attempts FROM outbox WHERE status='pending' AND retry_at<=? ORDER BY CASE kind WHEN 'live' THEN 0 ELSE 1 END,second,id LIMIT 1", (time.time(),)).fetchone()
        if row is None:
            return False
        item_id, raw, attempts = row
        p = json.loads(self.cipher.decrypt(raw[:12], raw[12:], item_id.encode()))
        try:
            result = sender(p)
        except Exception as exc:
            # Do not write URLs, credentials or payload coordinates to logs.
            error = type(exc).__name__ + (': ' + str(exc) if isinstance(exc, ValueError) else '')
            with self.db:
                self.db.execute('UPDATE outbox SET attempts=?,retry_at=?,error=? WHERE id=?',
                                (attempts+1, time.time()+min(3600, 15*2**min(attempts, 8)), error, item_id))
            print(json.dumps({'upload_failed': item_id, 'error': error}), flush=True)
        else:
            with self.db:
                self.db.execute("UPDATE outbox SET status='sent',attempts=?,result=?,error=NULL,sent_at=? WHERE id=?",
                                (attempts+1, json.dumps(result), time.time(), item_id))
        return True

    def status(self):
        return {'counts': [dict(zip(('account','kind','status','count'), r)) for r in self.db.execute('SELECT account,kind,status,count(*) FROM outbox GROUP BY account,kind,status')],
                'storage': [dict(zip(('target','count'),r)) for r in self.db.execute("SELECT json_extract(result,'$.storage_target'),count(*) FROM outbox WHERE status='sent' GROUP BY 1")],
                'errors': [dict(zip(('error','count'),r)) for r in self.db.execute("SELECT error,count(*) FROM outbox WHERE status='pending' AND error IS NOT NULL GROUP BY error")],
                'sources': [dict(account=r[0], checked=r[1], **json.loads(r[2])) for r in self.db.execute('SELECT * FROM scans')]}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', default='/var/lib/startag-google-reporter')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--one', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    reporter = Reporter(args.state)
    if args.status:
        print(json.dumps(reporter.status(), ensure_ascii=False)); return
    import fcntl
    lock = open(Path(args.state) / 'run.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    next_scan = 0
    while True:
        if time.monotonic() >= next_scan:
            for i, directory in enumerate(SOURCES, 1):
                try:
                    email, devices = read_source(directory)
                    reporter.discover(i, email, devices)
                except Exception as exc:
                    print(json.dumps({'source_error': i, 'error': type(exc).__name__}), flush=True)
            print(json.dumps(reporter.status()), flush=True)
            next_scan = time.monotonic() + 15
        if args.prepare:
            return
        worked = reporter.flush_one()
        if args.one:
            print(json.dumps(reporter.status()), flush=True); return
        time.sleep(0.15 if worked else 1)

if __name__ == '__main__':
    main()
