"""Encrypted local storage for Windows and Linux deployments."""
import base64
import ctypes
import json
import os
import secrets
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


LINUX_MAGIC = b"FINDMY-LINUX-VAULT-1\n"


def _linux_key(root: Path, create: bool = False) -> bytes:
    configured = os.environ.get("FINDMY_VAULT_KEY", "").strip()
    if configured:
        try:
            key = base64.b64decode(configured + "=" * (-len(configured) % 4), altchars=b"-_", validate=True)
        except Exception:
            key = b""
        if len(key) != 32:
            raise RuntimeError("FINDMY_VAULT_KEY must be a base64-encoded 32-byte key")
        return key
    path = root / ".vault-key"
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError("Invalid Linux vault key")
        return key
    if not create or any(root.glob("*.vault")):
        raise RuntimeError("Linux vault key missing; restore the original key from backup")
    key = secrets.token_bytes(32)
    # The service uses one worker. Never overwrite an existing deployment key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
        handle.flush()
        os.fsync(handle.fileno())
    return key


def _linux_transform(raw: bytes, root: Path, decrypt: bool = False) -> bytes:
    key = _linux_key(root, create=not decrypt)
    if decrypt:
        start = len(LINUX_MAGIC)
        if not raw.startswith(LINUX_MAGIC) or len(raw) < start + 12 + 16:
            raise RuntimeError("Invalid Linux vault file")
        nonce = raw[start:start + 12]
        return AESGCM(key).decrypt(nonce, raw[start + 12:], LINUX_MAGIC)
    nonce = secrets.token_bytes(12)
    return LINUX_MAGIC + nonce + AESGCM(key).encrypt(nonce, raw, LINUX_MAGIC)


def transform(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Use the Linux vault transform on non-Windows systems")
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    fn = lib.CryptUnprotectData if decrypt else lib.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL("kernel32")
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.suffix = ".dpapi" if os.name == "nt" else ".vault"
        if os.name != "nt" and any(self.root.glob("*.dpapi")):
            raise RuntimeError("Windows DPAPI files need encrypted transfer before use on Linux")

    def _path(self, name: str) -> Path:
        return self.root / (name + self.suffix)

    def _transform(self, raw: bytes, decrypt: bool = False) -> bytes:
        return transform(raw, decrypt) if os.name == "nt" else _linux_transform(raw, self.root, decrypt)

    def read(self, name: str, default=None):
        path = self._path(name)
        if not path.exists():
            return default
        return json.loads(self._transform(path.read_bytes(), decrypt=True))

    def write(self, name: str, value):
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(self._transform(json.dumps(value).encode("utf-8")))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)

    def delete(self, name: str):
        self._path(name).unlink(missing_ok=True)

    def has_data(self) -> bool:
        return any(self.root.glob("*.dpapi")) or any(self.root.glob("*.vault"))
