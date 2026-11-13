import asyncio
import base64
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from findmy import (LocalAnisetteProvider, LoginState, KeyPair,
                    FindMyAccessory, SmsSecondFactorMethod, InvalidCredentialsError,
                    UnauthorizedError, InvalidStateError)
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .network import check_network
from .storage import Vault
from .account import DiagnosticAppleAccount as AsyncAppleAccount, AppleAuthenticationError
from .mac_anisette import MacAnisetteProvider, BINARY, AppleTransportError
from .scheduler import AutoFetch, now
from .history import accumulate, normalize
from .concurrency import QueryGate
from .public_web import PUBLIC_ORIGINS, PUBLIC_MAP_ORIGIN
from .exports import ExportInput, export_locations
from .unified_tracks import install as install_unified_tracks
from .polling_settings import install as install_polling
from .central_locations import backfill as central_backfill, persist as central_persist

SOURCE=os.environ.get("FINDMY_SOURCE","apple")
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("FINDMY_DATA_DIR", ROOT / "data"))
PORT = int(os.environ.get("FINDMY_PORT", "8765"))
# Upstream debug/error messages may contain Apple response bodies. Do not persist them.
logging.getLogger("findmy").disabled = True
for namespace in ("findmy", "anisette"):
    log = logging.getLogger(namespace)
    log.handlers = [logging.NullHandler()]
    log.propagate = False
    log.setLevel(logging.CRITICAL)


class Service:
    def __init__(self, data: Path):
        self.vault = Vault(data)
        self.libs = data / "ani_libs.bin"
        self.lock = QueryGate()
        self.query_concurrency = 10
        self.query_slots = asyncio.Semaphore(self.query_concurrency)
        self.device_query_locks = {}
        self.account = None
        self.methods = []
        self.devices = self.vault.read("devices", {})
        self.location_cache = self.vault.read("locations", {})
        migrated = {device_id: normalize(saved, now()) for device_id, saved in self.location_cache.items()}
        if migrated != self.location_cache:
            self.vault.write("locations", migrated)
            self.location_cache = migrated
        self.schedule = AutoFetch(self)
        self.token = secrets.token_urlsafe(32)
        self.restore_error = None
        self.last_auth_error = self.vault.read("auth-diagnostic")
        try:
            saved = self.vault.read("account")
            if saved:
                self.account = AsyncAppleAccount.from_json(saved, anisette_libs_path=self.libs)
        except Exception:
            self.restore_error = "会话读取失败；请检查加密密钥或 Windows 用户身份，必要时退出并重新登录。"

    def get_account(self):
        if self.account is None:
            provider = MacAnisetteProvider() if BINARY.is_file() else LocalAnisetteProvider(libs_path=self.libs)
            self.account = AsyncAppleAccount(provider)
        return self.account

    def save(self):
        if self.account:
            # Upstream includes the password for session refresh; encrypt the ENTIRE object.
            self.vault.write("account", self.account.to_json())

    def status(self):
        acc = self.account
        state = acc.login_state if acc else LoginState.LOGGED_OUT
        return {"state": state.name, "logged_in": state == LoginState.LOGGED_IN,
                "account": acc.account_name if state != LoginState.LOGGED_OUT else None,
                "restore_error": self.restore_error, "version": "0.10.2",
                "location_ready": state == LoginState.LOGGED_IN and getattr(acc, "has_findmy_token", True),
                "auth_backend": "mac-coreadi" if isinstance(getattr(acc, "_anisette", None), MacAnisetteProvider) else "legacy-android",
                "last_auth_error": self.last_auth_error}

    def device(self, device_id):
        if device_id not in self.devices:
            raise HTTPException(404, "设备不存在")
        item = self.devices[device_id]
        cls = KeyPair if item["key"]["type"] == "keypair" else FindMyAccessory
        return item, cls.from_json(item["key"])

    @asynccontextmanager
    async def query_slot(self, device_id):
        device_lock = self.device_query_locks.setdefault(device_id, asyncio.Lock())
        async with device_lock, self.query_slots, self.lock.shared():
            yield

    async def fetch_locations(self, device_id):
        """Caller holds a query slot (or the exclusive lock in offline tests)."""
        account = self.get_account()
        def check_account():
            if account.login_state != LoginState.LOGGED_IN:
                raise HTTPException(409, "请先完成 Apple ID 登录及双重验证")
            if not getattr(account, "has_findmy_token", True):
                raise HTTPException(409, "账号已登录，但 Apple 未签发 Find My 查询令牌，暂不能获取位置。")
        auth_lock = getattr(account, "_report_auth_lock", None)
        if auth_lock is not None:
            async with auth_lock:
                check_account()
        else:
            check_account()
        item, key = self.device(device_id)
        try:
            reports = await asyncio.wait_for(self.account.fetch_location_history(key), timeout=120)
        except AppleTransportError as exc:
            if exc.status == 429:
                self.schedule.cooldown_until = max(self.schedule.cooldown_until, now() + 900)
                self.schedule.save()
                self.schedule.wake.set()
            raise
        finally:
            auth_lock = getattr(self.account, "_report_auth_lock", None)
            if auth_lock is not None:
                async with auth_lock:
                    self.save()
            else:
                self.save()
        points = [{"timestamp": r.timestamp.astimezone(timezone(timedelta(hours=8))).isoformat(),
                   "latitude": r.latitude, "longitude": r.longitude,
                   "accuracy_m": r.horizontal_accuracy, "confidence": r.confidence,
                   "status": r.status} for r in sorted(reports, key=lambda r: r.timestamp, reverse=True)]
        result = accumulate(self.location_cache.get(device_id), device_id, item["name"], points, now())
        updated = {**self.location_cache, device_id: result}
        self.vault.write("locations", updated)
        self.location_cache = updated
        await asyncio.to_thread(central_persist, self, SOURCE, {"id": device_id, **item}, points, result["reports"])
        return result


@asynccontextmanager
async def lifespan(app):
    app.state.service = Service(DATA)
    s = app.state.service
    await asyncio.to_thread(central_backfill, s, SOURCE)
    s.schedule.start()
    try:
        yield
    finally:
        await s.schedule.stop()
        if s.account:
            try:
                s.save()
            finally:
                await s.account.close()


app = FastAPI(title="FindMy 本地服务", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
install_unified_tracks(app, SOURCE)
install_polling(app)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def local_guard(request: Request, call_next):
    # The third-party map SDK runs on localhost. Do not let that origin read
    # the main page's injected token or call any API on the same host.
    if request.headers.get("host", "").split(":", 1)[0].lower() == "localhost" and (
        request.url.path != "/baidu-map" or request.method not in ("GET", "HEAD")
    ):
        return JSONResponse({"detail": "此地址仅用于地图"}, status_code=403)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        allowed = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", *PUBLIC_ORIGINS}
        if origin and origin not in allowed:
            return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
    if request.url.path.startswith("/api/"):
        token = request.headers.get("x-local-token", "")
        if not secrets.compare_digest(token, request.app.state.service.token):
            return JSONResponse({"detail": "请从本地页面操作（刷新页面后重试）"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 100_000:
            return JSONResponse({"detail": "请求过大"}, status_code=413)
    response = await call_next(request)
    frame_parents = ' '.join(sorted(PUBLIC_ORIGINS)) + ' ' + os.environ.get('FINDMY_MAP_PARENTS', 'http://127.0.0.1:8767 http://127.0.0.1:8769 http://127.0.0.1:8771')
    frame_source = PUBLIC_MAP_ORIGIN
    response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    if request.url.path == "/baidu-map":
        response.headers.update({"Referrer-Policy": "origin",
            "Content-Security-Policy": f"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' https://api.map.baidu.com https://*.bdimg.com; style-src 'unsafe-inline' https://api.map.baidu.com; img-src https://*.bdimg.com https://api.map.baidu.com data:; connect-src https://api.map.baidu.com https://*.bdimg.com; frame-ancestors http://127.0.0.1:{PORT} http://localhost:{PORT} {frame_parents}"})
    else:
        response.headers.update({"Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https://a.tile.openstreetmap.fr; connect-src 'self'; frame-src http://127.0.0.1:{PORT} http://localhost:{PORT} {frame_source}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # FastAPI's default errors echo input values, potentially including passwords/keys.
    return JSONResponse({"detail": "输入格式不正确，请检查必填项、验证码及密钥格式。"}, status_code=422)


@app.exception_handler(Exception)
async def safe_error(request, exc):
    if isinstance(exc, AppleTransportError):
        return JSONResponse({"detail": "Apple 限制了请求频率（HTTP 429），请停止重试并稍后再试。" if exc.status == 429
            else f"Apple 服务返回 HTTP {exc.status}，当前操作未完成。"}, status_code=502)
    if isinstance(exc, AppleAuthenticationError):
        diagnostic = exc.diagnostic()
        s = request.app.state.service
        s.last_auth_error = diagnostic
        s.vault.write("auth-diagnostic", diagnostic)
        return JSONResponse(diagnostic, status_code=502)
    messages = {InvalidCredentialsError: "Apple 拒绝了账号认证；此错误不足以断定密码不正确，请检查认证阶段及账号状态。",
                UnauthorizedError: "Apple 拒绝授权，请重新登录或检查账号状态。",
                InvalidStateError: "登录状态已变化，请刷新页面后继续。",
                TimeoutError: "请求超时，请检查网络后重试。"}
    return JSONResponse({"detail": messages.get(type(exc),
        f"操作未完成（{type(exc).__name__}）。请检查网络与账号状态后重试。")}, status_code=502)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    return html.replace("__LOCAL_TOKEN__", request.app.state.service.token).replace("__PUBLIC_MAP_ORIGIN__", PUBLIC_MAP_ORIGIN)


@app.get("/baidu-map", response_class=HTMLResponse)
async def baidu_map():
    return (ROOT / "static/baidu_map.html").read_text(encoding="utf-8")


@app.get("/assets/{name}")
async def asset(name: str):
    if name not in ("app.js", "style.css", "unified-track.js", "unified-track.css", "database-tools.js", "polling-settings.js"):
        raise HTTPException(404)
    return FileResponse(ROOT / "static" / name)


@app.get("/api/status")
async def status(request: Request):
    return request.app.state.service.status()


@app.post("/api/network")
async def network(request: Request):
    result = await check_network()
    (DATA / "network-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


@app.post("/api/anisette")
async def anisette(request: Request):
    s = request.app.state.service
    async with s.lock:
        # This generates local device provisioning only; no Apple ID is required.
        try:
            headers = await s.get_account().get_anisette_headers()
            ok = bool(headers.get("X-Apple-I-MD") and headers.get("X-Apple-I-MD-M"))
        finally:
            s.save()
        return {"ready": ok, "message": "本地 Anisette 已就绪" if ok else "未生成有效认证数据"}


class LoginInput(BaseModel):
    username: str = Field(min_length=3, max_length=254)
    password: SecretStr


@app.post("/api/login")
async def login(body: LoginInput, request: Request):
    s = request.app.state.service
    async with s.lock:
        acc = s.get_account()
        if acc.login_state != LoginState.LOGGED_OUT:
            raise HTTPException(409, "已有登录流程，请继续验证或先退出。")
        s.last_auth_error = None
        s.vault.delete("auth-diagnostic")
        try:
            await acc.login(body.username.strip(), body.password.get_secret_value())
        finally:
            s.save()
        return s.status()


@app.get("/api/2fa/methods")
async def methods(request: Request):
    s = request.app.state.service
    async with s.lock:
        if s.get_account().login_state != LoginState.REQUIRE_2FA:
            raise HTTPException(409, "当前无需双重验证")
        s.methods = list(await s.account.get_2fa_methods())
        return [{"id": i, "label": f"短信 {m.phone_number}" if isinstance(m, SmsSecondFactorMethod)
                 else "受信任的 Apple 设备"} for i, m in enumerate(s.methods)]


class FactorInput(BaseModel):
    method_id: int = Field(ge=0)


class CodeInput(FactorInput):
    code: str = Field(pattern=r"^\d{6}$")


async def factor_action(s, method_id, code=None):
    async with s.lock:
        if s.get_account().login_state != LoginState.REQUIRE_2FA or method_id >= len(s.methods):
            raise HTTPException(409, "请刷新验证方式后重试")
        method = s.methods[method_id]
        try:
            if code is None:
                await method.request()
            else:
                await method.submit(code)
        finally:
            s.save()
        return s.status()


@app.post("/api/2fa/request")
async def request_code(body: FactorInput, request: Request):
    return await factor_action(request.app.state.service, body.method_id)


@app.post("/api/2fa/submit")
async def submit_code(body: CodeInput, request: Request):
    return await factor_action(request.app.state.service, body.method_id, body.code)


@app.post("/api/logout")
async def logout(request: Request):
    s = request.app.state.service
    s.schedule.pause()
    async with s.lock:
        if s.account:
            await s.account.close()
        s.account = None
        s.methods = []
        s.vault.delete("account")
        s.restore_error = None
        s.last_auth_error = None
        s.vault.delete("auth-diagnostic")
        return s.status()


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("empty name")
        return value


class ImportKey(NewKey):
    sn: str | None = Field(default=None, pattern=r"^[0-9]{12}$")
    private_key: SecretStr | None = None
    accessory: dict | None = None


class SnTaskInput(BaseModel):
    expected_pubkey: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{56}$")
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sn: str = Field(pattern=r"^[0-9]{12}$")
    interval_seconds: int | None = Field(default=None, ge=60, le=86400, strict=True)
    enabled: bool = True


def lookup_sn_record(sn):
    request = urllib.request.Request(
        f"https://console.startag.cc/php/getSnKey?sn={sn}",
        headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("oversized response")
    return json.loads(raw)


def device_summary(device_id, item):
    result = {"id": device_id, "name": item["name"], "sn": item.get("sn"), "kind": item["key"]["type"]}
    if result["kind"] == "keypair":
        key = KeyPair.from_json(item["key"])
        result.update(public_key=key.adv_key_b64, public_key_hex=key.adv_key_bytes.hex(),
                      hashed_public_key=key.hashed_adv_key_b64, mac_address=key.mac_address,
                      advertisement_hex=key.adv_data().hex())
    return result


def add_device(s, name, key, sn=None):
    device_id = str(uuid4())
    item = {"name": name, "key": key.to_json()}
    if sn is not None:
        item["sn"] = sn
    new = {**s.devices, device_id: item}
    s.vault.write("devices", new)
    s.devices = new
    return device_summary(device_id, item)


@app.get("/api/devices")
async def devices(request: Request):
    return [device_summary(k, v) for k, v in request.app.state.service.devices.items()]


@app.post("/api/keys/generate")
async def generate(body: NewKey, request: Request):
    s = request.app.state.service
    async with s.lock:
        key = KeyPair.new()
        key.name = body.name
        return add_device(s, body.name, key)


@app.post("/api/keys/import")
async def import_key(body: ImportKey, request: Request):
    try:
        if body.private_key is not None and body.accessory is None:
            raw = base64.b64decode(body.private_key.get_secret_value().strip(), validate=True)
            if len(raw) != 28:
                raise ValueError()
            key = KeyPair(raw, name=body.name)
        elif body.accessory is not None and body.private_key is None:
            if body.accessory.get("type") != "accessory":
                raise ValueError()
            key = FindMyAccessory.from_json(body.accessory)
        else:
            raise ValueError()
    except Exception:
        raise HTTPException(422, "需要有效的 28 字节 Base64 私钥或 FindMy.py accessory JSON。") from None
    s = request.app.state.service
    async with s.lock:
        if body.sn:
            for ident,item in s.devices.items():
                same_sn=item.get('sn')==body.sn
                same_key=item['key']['type']=='keypair' and KeyPair.from_json(item['key']).adv_key_b64==key.adv_key_b64
                if same_sn and not same_key:raise HTTPException(409,'SN 已存在且密钥不一致')
                if same_key:
                    if item.get('sn') not in (None,body.sn):raise HTTPException(409,'密钥已关联其他 SN')
                    updated={**item,'sn':body.sn,'name':body.name}
                    values={**s.devices,ident:updated};s.vault.write('devices',values);s.devices=values
                    return device_summary(ident,updated)
        return add_device(s, body.name, key, sn=body.sn)


@app.post("/api/keys/import-sn")
async def import_sn(body: SnTaskInput, request: Request):
    s = request.app.state.service
    if body.enabled and not s.status()["location_ready"]:
        raise HTTPException(409, "请先登录并取得位置查询权限，再启动自动拉取。")
    try:
        response = await asyncio.to_thread(lookup_sn_record, body.sn)
    except (OSError, ValueError):
        raise HTTPException(502, "SN 密钥服务暂时不可用，请稍后重试。") from None
    if not isinstance(response, dict) or response.get("code") != 200:
        raise HTTPException(404, "未查到该 SN 的密钥。")
    data = response.get("data")
    try:
        if not isinstance(data, dict) or str(data.get("sn")) != body.sn:
            raise ValueError()
        raw = base64.b64decode(data["private_key"], validate=True)
        if len(raw) != 28:
            raise ValueError()
        key = KeyPair(raw, name=body.sn)
        if key.adv_key_b64 != data.get("public_key"):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(502, "SN 密钥数据校验失败，未导入设备。") from None
    if body.expected_pubkey and key.adv_key_bytes.hex().upper() != body.expected_pubkey.upper():
        raise HTTPException(409, "SN 密钥服务返回的公钥与输入公钥不一致，未添加设备")
    async with s.lock:
        for old in s.devices.values():
            if old.get("sn") == body.sn and (old["key"]["type"] != "keypair" or KeyPair.from_json(old["key"]).adv_key_b64 != key.adv_key_b64):
                raise HTTPException(409, "已有 SN 的公钥与密钥服务不一致，未修改设备")
        existing_id = next((device_id for device_id, item in s.devices.items()
            if item["key"]["type"] == "keypair"
            and KeyPair.from_json(item["key"]).adv_key_b64 == key.adv_key_b64), None)
        if body.expected_pubkey and existing_id is not None and s.devices[existing_id].get("sn") not in (None, body.sn):
            raise HTTPException(409, "该公钥已属于另一个 SN，未修改原设备")
        created = existing_id is None
        if created:
            summary = add_device(s, (body.name or body.sn).strip() or body.sn, key, sn=body.sn)
            device_id = summary["id"]
        else:
            device_id = existing_id
            item = s.devices[device_id]
            if item.get("sn") != body.sn:
                devices = {**s.devices, device_id: {**item, "sn": body.sn}}
                s.vault.write("devices", devices)
                s.devices = devices
            summary = device_summary(device_id, s.devices[device_id])
        if body.name and s.devices[device_id]["name"] != body.name.strip():
            devices = {**s.devices, device_id: {**s.devices[device_id], "name": body.name.strip() or body.sn}}
            s.vault.write("devices", devices); s.devices = devices
            summary = device_summary(device_id, s.devices[device_id])
        s.schedule.configure(device_id, body.interval_seconds, body.enabled)
    return {"device": summary, "created": created}


@app.post("/api/devices/{device_id}/export")
async def export_key(device_id: str, request: Request):
    item, key = request.app.state.service.device(device_id)
    return {"name": item["name"], "key": key.to_json()}


@app.post("/api/devices/{device_id}/locations")
async def locations(device_id: str, request: Request):
    s = request.app.state.service
    async with s.query_slot(device_id):
        if s.schedule.cooldown_until > now():
            raise HTTPException(429, "Apple 限流冷却中，请稍后查询。")
        return await s.fetch_locations(device_id)


@app.get("/api/devices/{device_id}/locations")
async def cached_locations(device_id: str, request: Request):
    s = request.app.state.service
    item, _ = s.device(device_id)
    cached = s.location_cache.get(device_id)
    return {**cached, "device": item["name"]} if cached else None


class BatchRenameItem(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=80)


class BatchRenameInput(BaseModel):
    items: list[BatchRenameItem] = Field(min_length=1, max_length=500)


@app.post("/api/devices/rename-batch")
async def rename_batch(body: BatchRenameInput, request: Request):
    """Rename many devices under a single write lock; the per-device route waits for
    every in-flight query to drain, which makes bulk renames take tens of minutes."""
    s = request.app.state.service
    results = []
    async with s.lock:
        devices = dict(s.devices)
        for entry in body.items:
            item = devices.get(entry.device_id)
            if item is None:
                results.append({"device_id": entry.device_id, "ok": False, "detail": "设备不存在"})
                continue
            name = entry.name.strip()
            if not name:
                results.append({"device_id": entry.device_id, "ok": False, "detail": "名称为空"})
                continue
            cls = KeyPair if item["key"]["type"] == "keypair" else FindMyAccessory
            key = cls.from_json(item["key"])
            key.name = name
            devices[entry.device_id] = {**item, "name": name, "key": key.to_json()}
            results.append({"device_id": entry.device_id, "ok": True, "name": name})
        if any(result["ok"] for result in results):
            s.vault.write("devices", devices)
            s.devices = devices
    return {"renamed": sum(1 for result in results if result["ok"]),
            "failed": sum(1 for result in results if not result["ok"]), "results": results}


@app.post("/api/devices/{device_id}/rename")
async def rename(device_id: str, body: NewKey, request: Request):
    s = request.app.state.service
    async with s.lock:
        item, key = s.device(device_id)
        key.name = body.name
        updated = {**item, "name": body.name, "key": key.to_json()}
        devices = {**s.devices, device_id: updated}
        s.vault.write("devices", devices)
        s.devices = devices
        return device_summary(device_id, updated)


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str, request: Request):
    s = request.app.state.service
    async with s.lock:
        s.device(device_id)
        s.schedule.remove(device_id)
        devices = dict(s.devices)
        del devices[device_id]
        s.vault.write("devices", devices)
        s.devices = devices
        return {"deleted_id": device_id, "history_preserved": device_id in s.location_cache}


class ScheduleInput(BaseModel):
    device_id: str
    interval_seconds: int | None = Field(default=None, ge=60, le=86400, strict=True)
    enabled: bool


@app.get("/api/schedule")
async def schedule_status(request: Request):
    return request.app.state.service.schedule.status()


@app.post("/api/schedule")
async def configure_schedule(body: ScheduleInput, request: Request):
    return request.app.state.service.schedule.configure(body.device_id, body.interval_seconds, body.enabled)


class PauseInput(BaseModel):
    device_id: str


@app.post("/api/schedule/pause")
async def pause_schedule(body: PauseInput, request: Request):
    return request.app.state.service.schedule.pause(body.device_id)


@app.post("/api/locations/export")
async def export_history(body: ExportInput, request: Request):
    return export_locations(request.app.state.service, body)
