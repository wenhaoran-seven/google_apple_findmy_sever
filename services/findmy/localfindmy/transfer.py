"""Password-protected cross-machine transfer; never includes Apple credentials."""
import json
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .storage import Vault

MAGIC = b"FINDMY-TRANSFER-1\n"
NAMES = ("devices", "locations", "schedule")


def key(password, salt):
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))


def seal(data, password):
    if len(password) < 12:
        raise ValueError("Migration password must contain at least 12 characters")
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    plain = json.dumps({"version": 1, **{name: data.get(name, {}) for name in NAMES}}).encode()
    return MAGIC + salt + nonce + AESGCM(key(password, salt)).encrypt(nonce, plain, MAGIC)


def unseal(blob, password):
    if not blob.startswith(MAGIC):
        raise ValueError("Unsupported migration file")
    payload = blob[len(MAGIC):]
    data = json.loads(AESGCM(key(password, payload[:16])).decrypt(payload[16:28], payload[28:], MAGIC))
    if data.get("version") != 1 or any(not isinstance(data.get(name), dict) for name in NAMES):
        raise ValueError("Invalid migration data")
    return {name: data[name] for name in NAMES}


def restore(blob, password, destination):
    root = Path(destination)
    if Vault(root).has_data():
        raise ValueError("Destination already contains data. Use a freshly extracted folder.")
    data = unseal(blob, password)
    schedule = data["schedule"]
    if schedule.get("version") == 2:
        for task in schedule.get("tasks", {}).values():
            task.update(enabled=False, next_run=None)
        schedule["cooldown_until"] = 0
    elif schedule:
        schedule.update(enabled=False, next_run=None)
    vault = Vault(root)
    created = []
    try:
        for name in NAMES:
            vault.write(name, data[name])
            created.append(name)
    except Exception:
        for name in created:
            vault.delete(name)
        raise
    return {"devices": len(data["devices"]),
            "reports": sum(len(item.get("reports", [])) for item in data["locations"].values())}
