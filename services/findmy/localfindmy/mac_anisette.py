"""Local, serialized Mac CoreADI emulation; state stays in the encrypted vault."""
import asyncio
import os
import base64
import hashlib
import plistlib
import secrets
import time
from pathlib import Path

import aiohttp
from findmy import BaseAnisetteProvider
from findmy.util.http import HttpSession
from findmy.util.files import read_data_json, save_and_return_json

from ._macadi.adi_run import CoreADI
from .transport import apple_proxy_for_url

BINARY = Path(os.environ.get("FINDMY_ADI_BINARY", Path(__file__).resolve().parent.parent / "data/libs/CoreADI.x86_64"))
BINARY_SHA256 = "2eafc3c79c7f41a8afbfbeaaf23ee5dab5ad5bda55ec8e54dbc0ddf1d20ce630"
PLATFORM = "<MacBookPro13,2> <macOS;13.1;22C65>"
STORE = "/Users/Shared/adi/"


class MacADIError(RuntimeError):
    pass


class AppleTransportError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Apple HTTP {status}")


class AppleHttpSession(HttpSession):
    """Keep TLS verification and use fresh connections, including between SRP messages."""
    def __init__(self, platform=None):
        super().__init__()
        self.platform = platform

    async def _get_session(self):
        if self._closed:
            raise RuntimeError("Session closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30),
                connector=aiohttp.TCPConnector(force_close=True), trust_env=False)
        return self._session

    async def request(self, method, url, **kwargs):
        proxy = apple_proxy_for_url(url)
        if proxy:
            kwargs["proxy"] = proxy
        headers = dict(kwargs.get("headers", {}))
        if self.platform:
            for name, value in headers.items():
                if name.lower() == "x-mme-client-info" and " <" in value:
                    headers[name] = self.platform + " <" + value.rsplit(" <", 1)[1]
            kwargs["headers"] = headers
        response = await super().request(method, url, **kwargs)
        if response.status_code == 429:
            raise AppleTransportError(429)
        return response


class MacAnisetteProvider(BaseAnisetteProvider):
    def __init__(self, state=None):
        super().__init__()
        self.identity = state["identity"] if state else {
            "device_id": "00006000-" + secrets.token_hex(8).upper(),
            "local_user_uuid": secrets.token_hex(16).upper(),
            "host_uuid": secrets.token_hex(16),
            "serial": "C02" + ''.join(secrets.choice('0123456789ABCDEFGHJKLMNPQRSTUVWXYZ') for _ in range(9)),
        }
        self.files = state.get("files", {}) if state else {}
        self._adi = None
        self._data = {}
        self._http = AppleHttpSession()
        self._lock = asyncio.Lock()

    def _run(self, name, *args):
        if self._adi is None:
            if not BINARY.is_file() or hashlib.sha256(BINARY.read_bytes()).hexdigest() != BINARY_SHA256:
                raise MacADIError("Missing or unsupported Mac CoreADI binary")
            self._adi = CoreADI(path=BINARY, log=False)
            self._adi.stubs.hostuuid = bytes.fromhex(self.identity["host_uuid"])
            for path, content in self.files.items():
                if path.startswith(STORE) and path != STORE + "adi.pb":
                    self._adi.stubs.files[path] = bytearray(base64.b64decode(content, validate=True))
            result = self._adi.initialize()
            if not result.ok:
                raise MacADIError("Mac ADI initialization failed")
        try:
            return getattr(self._adi, name)(*args)
        finally:
            self.files = {k: base64.b64encode(bytes(v)).decode() for k, v in self._adi.stubs.files.items() if k.startswith(STORE)}

    async def _call(self, name, *args):
        result = await asyncio.to_thread(self._run, name, *args)
        if not result.ok:
            raise MacADIError(f"{name} failed ({result.ret})")
        return result.outputs

    async def _provision(self):
        probe = await asyncio.to_thread(self._run, "is_machine_provisioned", -2)
        if probe.ret == 0:
            return
        if probe.ret != -45061:
            raise MacADIError(f"Unexpected provisioning state ({probe.ret})")
        headers = {
            "User-Agent": "akd/1.0 CFNetwork/1404.0.5 Darwin/22.3.0",
            "X-MMe-Client-Info": self.client,
            "X-Mme-Device-Id": self.identity["device_id"],
            "X-Apple-I-MD-LU": self.identity["local_user_uuid"],
            "X-Apple-I-SRL-NO": self.identity["serial"],
            "X-Apple-Client-App-Name": "Setup",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        lookup = await self._http.get("https://gsa.apple.com/grandslam/GsService2/lookup", headers=headers)
        if not lookup.ok:
            raise AppleTransportError(lookup.status_code)
        urls = lookup.plist()["urls"]

        async def post(url, body):
            # Only send provisioning material to HTTPS Apple endpoints from the verified bag.
            from urllib.parse import urlsplit
            target = urlsplit(url)
            if target.scheme != "https" or not (target.hostname or "").endswith(".apple.com"):
                raise MacADIError("Unexpected provisioning endpoint")
            response = await self._http.post(url, headers={**headers, "X-Apple-I-Client-Time": self.timestamp},
                data=plistlib.dumps({"Header": {}, "Request": body}))
            if not response.ok:
                raise AppleTransportError(response.status_code)
            return response.plist()["Response"]

        start = await post(urls["midStartProvisioning"], {})
        outputs = await self._call("provisioning_start", -2, base64.b64decode(start["spim"]))
        finish = await post(urls["midFinishProvisioning"], {"cpim": base64.b64encode(outputs["cpim"]).decode()})
        await self._call("provisioning_end", outputs["session_id"], base64.b64decode(finish["ptm"]), base64.b64decode(finish["tk"]))
        routing = int(finish.get("X-Apple-I-MD-RINFO", 0))
        await self._call("set_routing", -2, routing)
        self.identity["provisioned_at"] = time.time()

    @property
    def client(self):
        return PLATFORM + " <com.apple.AuthKit/1 (com.apple.akd/1.0)>"

    @property
    def otp(self):
        return self._data.get("otp", "")

    @property
    def machine(self):
        return self._data.get("machine", "")

    @property
    def router(self):
        return self._data.get("routing", "0")

    async def get_headers(self, user_id, device_id, serial="0", with_client_info=False):
        async with self._lock:
            # This upstream harness uses an allocation-only heap. Reboot the emulator
            # for each OTP while keeping the SAME provisioned machine and saved files.
            # Reusing its heap exhausts virtual memory after about seven OTP calls.
            self._adi = None
            try:
                await self._provision()
                outputs = await self._call("request_otp", -2)
                routing = await self._call("get_routing", -2)
                self._data = {"otp": base64.b64encode(outputs["otp"]).decode(),
                    "machine": base64.b64encode(outputs["mid"]).decode(), "routing": str(routing.get("routing", 0))}
                headers = await super().get_headers(self.identity["local_user_uuid"], self.identity["device_id"], self.identity["serial"], with_client_info)
                if with_client_info:
                    headers["X-Mme-Client-Info"] = PLATFORM + " <com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>"
                return headers
            finally:
                self._adi = None
                # Unicorn hooks form cycles; reclaim released emulators promptly.
                import gc
                await asyncio.to_thread(gc.collect)

    def to_json(self, dst=None):
        return save_and_return_json({"type": "aniMacLocal", "identity": self.identity, "files": self.files}, dst)

    @classmethod
    def from_json(cls, val):
        return cls(read_data_json(val))

    async def close(self):
        await self._http.close()
        self._adi = None
