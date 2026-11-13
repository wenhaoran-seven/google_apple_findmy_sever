"""Encrypted values with atomic SQLite transactions, shared by web and worker."""
import json
from contextlib import contextmanager
import os
import secrets
import sqlite3
import time
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('GOOGLEFINDMY_DATA', ROOT / 'data'))


class Store:
    def __init__(self, directory=DATA):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.directory / 'state.sqlite3'
        key_path = self.directory / '.vault-key'
        if not key_path.exists():
            if self.db.exists():
                raise RuntimeError('加密密钥缺失，不能创建新密钥覆盖现有数据')
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as out:
                    out.write(secrets.token_bytes(32))
        self.cipher = AESGCM(key_path.read_bytes())
        with self.connect() as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('CREATE TABLE IF NOT EXISTS kv (name TEXT PRIMARY KEY, value BLOB NOT NULL)')
            # Retain issued numbers even if a device is later removed. Allocation
            # and encrypted device insertion share one SQLite write transaction.
            con.execute('CREATE TABLE IF NOT EXISTS device_serials '
                        '(sn TEXT PRIMARY KEY, device_id TEXT UNIQUE NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS device_serial_history '
                        '(sn TEXT PRIMARY KEY, device_id TEXT NOT NULL)')
            con.execute('INSERT OR IGNORE INTO device_serial_history SELECT sn, device_id FROM device_serials')
        self._initialize_indexes()
        if os.name != 'nt':
            self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db, timeout=20)
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback(); raise
        finally:
            con.close()

    def get(self, name, default=None):
        with self.connect() as con:
            row = con.execute('SELECT value FROM kv WHERE name=?', (name,)).fetchone()
        if row is None:
            return default
        raw = row[0]
        return json.loads(self.cipher.decrypt(raw[:12], raw[12:], name.encode()))

    def put(self, name, value):
        nonce = secrets.token_bytes(12)
        raw = nonce + self.cipher.encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), name.encode())
        with self.connect() as con:
            con.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (name, raw))
            self._index_value(con, name, value)

    def names(self, prefix):
        with self.connect() as con:
            return [r[0] for r in con.execute('SELECT name FROM kv WHERE name LIKE ? ORDER BY name', (prefix+'%',))]

    def all(self, prefix):
        with self.connect() as con:
            rows = con.execute('SELECT name,value FROM kv WHERE name LIKE ? ORDER BY name', (prefix+'%',)).fetchall()
        return [json.loads(self.cipher.decrypt(raw[:12], raw[12:], name.encode())) for name,raw in rows]

    def _encrypted(self, name, value):
        nonce = secrets.token_bytes(12)
        return nonce + self.cipher.encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), name.encode())

    def queue_deletion(self, device, job):
        device = {**device, 'enabled': False, 'delete_state': 'pending',
                  'delete_error': None, 'delete_requested_at': time.time()}
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            for name, value in [('device:'+device['id'], device), ('job:'+job['id'], job)]:
                con.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (name, self._encrypted(name, value)))
                self._index_value(con, name, value)

    def archive_deleted_device(self, device_id):
        """Keep encrypted recovery data and issued SN; remove only the active record."""
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            values = {}
            for prefix in ('device:', 'history:'):
                name = prefix+device_id
                row = con.execute('SELECT value FROM kv WHERE name=?', (name,)).fetchone()
                if row:
                    raw = row[0]
                    values[prefix[:-1]] = json.loads(self.cipher.decrypt(raw[:12], raw[12:], name.encode()))
            if 'device' not in values:
                return
            values.update(deleted_at=time.time(), cloud_absence_confirmed=True)
            name = 'deleted_device:'+device_id
            con.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (name, self._encrypted(name, values)))
            con.execute('DELETE FROM kv WHERE name IN (?, ?)', ('device:'+device_id, 'history:'+device_id))

    def save_device_with_sn(self, device):
        from keys import address_for_sn
        device = dict(device)
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT sn FROM device_serials WHERE device_id=?',
                              (device['id'],)).fetchone()
            if row:
                sn = row[0]
            else:
                sn = device.get('sn')
                if sn is None:
                    candidate = 100000000000
                    for (issued,) in con.execute('SELECT sn FROM device_serial_history ORDER BY sn'):
                        if int(issued) > candidate:
                            break
                        candidate = max(candidate, int(issued) + 1)
                    sn = str(candidate)
                if not isinstance(sn, str) or len(sn) != 12 or not sn.isascii() or not sn.isdecimal() or sn[0] == '0':
                    raise ValueError('SN 必须是首位非零的 12 位数字，或编号空间已耗尽')
                owner = con.execute('SELECT device_id FROM device_serial_history WHERE sn=?', (sn,)).fetchone()
                if owner and owner[0] != device['id']:
                    raise ValueError('此 SN 已被其他设备使用，请换一个 SN')
                con.execute('INSERT INTO device_serials VALUES (?, ?)', (sn, device['id']))
                con.execute('INSERT OR IGNORE INTO device_serial_history VALUES (?, ?)', (sn, device['id']))
            device['sn'] = sn
            if not row or not device.get('static_random_address'):
                device['static_random_address'] = address_for_sn(sn)
            name = 'device:' + device['id']
            nonce = secrets.token_bytes(12)
            raw = nonce + self.cipher.encrypt(nonce, json.dumps(device, ensure_ascii=False).encode(), name.encode())
            con.execute('INSERT OR REPLACE INTO kv VALUES (?, ?)', (name, raw))
        return device

    def update_device_sn(self, device_id, sn):
        """Change local serial metadata atomically; preserve radio/cloud identity."""
        if not isinstance(sn, str) or len(sn) != 12 or not sn.isascii() or not sn.isdecimal() or sn[0] == '0':
            raise ValueError('SN 必须是首位非零的 12 位数字')
        name = 'device:' + device_id
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT value FROM kv WHERE name=?', (name,)).fetchone()
            if row is None:
                raise KeyError(device_id)
            raw = row[0]
            device = json.loads(self.cipher.decrypt(raw[:12], raw[12:], name.encode()))
            if device.get('delete_state'):
                raise ValueError('此设备等待确认注销，暂不能修改 SN')
            owner = con.execute('SELECT device_id FROM device_serial_history WHERE sn=?', (sn,)).fetchone()
            if owner and owner[0] != device_id:
                raise ValueError('此 SN 已被其他设备使用，请换一个 SN')
            con.execute('INSERT OR IGNORE INTO device_serial_history VALUES (?, ?)', (device['sn'], device_id))
            con.execute('INSERT OR IGNORE INTO device_serial_history VALUES (?, ?)', (sn, device_id))
            con.execute('UPDATE device_serials SET sn=? WHERE device_id=?', (sn, device_id))
            device['sn'] = sn
            con.execute('UPDATE kv SET value=? WHERE name=?', (self._encrypted(name, device), name))
        return device

    @staticmethod
    def _index_value(con, name, value):
        if name.startswith('job:'):
            result = value.get('result') or {}
            con.execute('INSERT OR REPLACE INTO job_index VALUES(?,?,?,?,?)',
                        (name, value.get('created_at', 0), value.get('state'), value.get('operation'), int(result.get('new_count') or 0)))
        elif name.startswith('history:'):
            latest = max((p['timestamp'] for p in value if isinstance(p.get('timestamp'), (int, float))), default=None)
            con.execute('INSERT OR REPLACE INTO history_index VALUES(?,?,?)', (name[8:], len(value), latest))

    def _initialize_indexes(self):
        with self.connect() as con:
            con.execute('CREATE TABLE IF NOT EXISTS store_indexes(version INTEGER PRIMARY KEY)')
            con.execute('CREATE TABLE IF NOT EXISTS job_index(name TEXT PRIMARY KEY,created_at REAL,state TEXT,operation TEXT,new_count INTEGER)')
            con.execute('CREATE INDEX IF NOT EXISTS jobs_recent ON job_index(created_at DESC)')
            con.execute("CREATE INDEX IF NOT EXISTS jobs_new_locations ON job_index(created_at DESC) WHERE operation='locate' AND state='success' AND new_count>0")
            con.execute('CREATE INDEX IF NOT EXISTS jobs_state ON job_index(state)')
            con.execute('CREATE TABLE IF NOT EXISTS history_index(device_id TEXT PRIMARY KEY,count INTEGER,latest REAL)')
            if not con.execute('SELECT 1 FROM store_indexes WHERE version=1').fetchone():
                for name, raw in con.execute("SELECT name,value FROM kv WHERE name LIKE 'job:%' OR name LIKE 'history:%'"):
                    value = json.loads(self.cipher.decrypt(raw[:12], raw[12:], name.encode()))
                    self._index_value(con, name, value)
                con.execute('INSERT INTO store_indexes VALUES(1)')

    def recent_jobs(self, limit=30, locations=False, unfinished=False):
        where = "WHERE state IN ('queued','running')" if unfinished else "WHERE operation='locate' AND state='success' AND new_count>0" if locations else ''
        suffix = '' if unfinished else ' LIMIT ?'
        with self.connect() as con:
            rows = con.execute('SELECT kv.name,kv.value FROM job_index j JOIN kv ON kv.name=j.name '+where+' ORDER BY j.created_at DESC'+suffix, () if unfinished else (limit,)).fetchall()
        return [json.loads(self.cipher.decrypt(raw[:12],raw[12:],name.encode())) for name,raw in rows]

    def history_stats(self):
        with self.connect() as con:
            return {ident: (count, latest) for ident,count,latest in con.execute('SELECT device_id,count,latest FROM history_index')}
