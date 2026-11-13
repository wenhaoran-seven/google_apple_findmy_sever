"""Preserve safe Apple status codes instead of mislabelling every refusal as a bad password."""
import re
import asyncio
import time
import base64
from datetime import datetime, timedelta, timezone
from findmy import AsyncAppleAccount as UpstreamAppleAccount
from findmy import AsyncTrustedDeviceSecondFactor, LoginState, UnhandledProtocolError, UnauthorizedError, InvalidStateError
from findmy.reports.reports import LocationReport
from findmy.util.files import read_data_json

from .mac_anisette import MacAnisetteProvider, AppleHttpSession, AppleTransportError, PLATFORM


class AppleAuthenticationError(Exception):
    def __init__(self, stage, code):
        self.stage = stage if stage in ("init", "complete") else "unknown"
        self.code = code if type(code) is int else None
        super().__init__("Apple authentication request rejected")

    def diagnostic(self):
        phase = {"init": "登录初始化（尚未校验密码）", "complete": "密码挑战", "unknown": "账号认证"}[self.stage]
        if self.code == -80045:
            detail = (
                f"Apple 拒绝了{phase}，错误码 -80045（This action cannot be completed at this time）。"
                "这不是密码错误的证据；当前 FindMy.py 0.10.2 / Anisette 认证方式存在兼容问题。"
                "请勿连续重试或因此修改密码，需要更换兼容的认证后端。"
            )
        else:
            detail = f"Apple 在{phase}阶段拒绝请求（错误码 {self.code}）。请结合账号状态排查，不能仅据此认定密码错误。"
        return {"stage": self.stage, "apple_code": self.code, "detail": detail}


class AppleAuthenticationTransportError(AppleAuthenticationError):
    """Only a stage and numeric HTTP status; never Apple response bodies."""
    def __init__(self, stage, http_status):
        super().__init__(stage, None)
        self.http_status = http_status

    def diagnostic(self):
        phase = {"init": "登录初始化（尚未校验密码）", "complete": "密码挑战", "unknown": "账号认证"}[self.stage]
        if self.http_status == 503:
            explanation = "苹果登录接口当前不可用，可能与服务端或当前网络出口有关；不能据此判断账号或密码错误。请稍后或使用可用的可信网络出口重试。"
        elif self.http_status == 429:
            explanation = "苹果限制了请求频率，请停止连续重试并稍后再试。"
        else:
            explanation = "苹果接口未接受本次请求；请检查网络和账号状态，不能仅据此判断密码错误。"
        return {"stage": self.stage, "http_status": self.http_status,
                "detail": f"Apple 在{phase}阶段返回 HTTP {self.http_status}。{explanation}"}


class DiagnosticAppleAccount(UpstreamAppleAccount):
    def __init__(self, anisette, *, state_info=None):
        super().__init__(anisette, state_info=state_info)
        self.auth_trace = []
        self._report_auth_lock = asyncio.Lock()
        self._report_auth_generation = 0
        self._report_auth_error = None
        if isinstance(anisette, MacAnisetteProvider):
            self._uid = anisette.identity["local_user_uuid"]
            self._devid = anisette.identity["device_id"]
            self._http = AppleHttpSession(platform=PLATFORM)

    @classmethod
    def from_json(cls, val, *, anisette_libs_path=None):
        state = read_data_json(val)
        if state.get("anisette", {}).get("type") == "aniMacLocal":
            return cls(MacAnisetteProvider.from_json(state["anisette"]), state_info=state)
        return super().from_json(state, anisette_libs_path=anisette_libs_path)

    @property
    def has_findmy_token(self):
        return bool(self._login_state_data.get("mobileme_data", {}).get("tokens", {}).get("searchPartyToken"))

    async def fetch_raw_reports(self, devices):
        """Use ACSN: v2 can return stale reports even for a current date window.

        Keep decryption and device/key matching in the upstream reports fetcher.
        Neither endpoint guarantees a complete history for a fixed key.
        """
        end = datetime.now(timezone.utc)
        data = {"search": [{
            "startDate": int((end - timedelta(days=7)).timestamp() * 1000),
            "endDate": int(end.timestamp() * 1000),
            "ids": primary, "secondaryIds": secondary,
        } for primary, secondary in devices]}

        for attempt in range(2):
            # OTP generation and session renewal share a lock, but HTTP requests
            # are independent. Capture credentials only after any renewal ends.
            async with self._report_auth_lock:
                if self._report_auth_error and self._report_auth_error[0] > time.monotonic():
                    raise self._report_auth_error[1]
                if self.login_state != LoginState.LOGGED_IN:
                    raise InvalidStateError("Location queries require login")
                generation = self._report_auth_generation
                auth = (self._login_state_data["dsid"],
                        self._login_state_data["mobileme_data"]["tokens"]["searchPartyToken"])
                headers = await self.get_anisette_headers()
            response = await self._http.post(
                "https://gateway.icloud.com/acsnservice/fetch",
                auth=auth, headers=headers, json=data)
            if response.status_code != 401:
                break
            if attempt:
                raise UnauthorizedError("Location query authorization rejected")
            async with self._report_auth_lock:
                if self._report_auth_error and self._report_auth_error[0] > time.monotonic():
                    raise self._report_auth_error[1]
                if generation == self._report_auth_generation:
                    try:
                        if await self._gsa_authenticate() != LoginState.AUTHENTICATED:
                            raise UnauthorizedError("Location query authorization rejected")
                        await self._login_mobileme()
                    except Exception as exc:
                        # Concurrent 401s must not start a refresh storm.
                        self._report_auth_error = (time.monotonic() + 60, exc)
                        raise
                    finally:
                        self._report_auth_generation += 1

        if not response.ok:
            raise AppleTransportError(response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise UnhandledProtocolError("Invalid location response") from None
        if (str(payload.get("statusCode")) != "200"
                or not isinstance(payload.get("results"), list)):
            raise UnhandledProtocolError("Location response not successful")
        return [LocationReport(base64.b64decode(item["payload"], validate=True),
                               base64.b64decode(item["id"], validate=True))
                for item in payload["results"]]

    async def _gsa_request(self, parameters):
        try:
            response = await super()._gsa_request(parameters)
        except AppleTransportError as exc:
            raise AppleAuthenticationTransportError(parameters.get("o"), exc.status) from None
        except UnhandledProtocolError as exc:
            # Match only this fixed upstream message. Never expose arbitrary exception text.
            match = re.fullmatch(r"Error response for GSA request: ([1-5][0-9]{2})", str(exc))
            if match:
                raise AppleAuthenticationTransportError(parameters.get("o"), int(match[1])) from None
            raise
        status = response.get("Status", {})
        code = status.get("ec")
        self.auth_trace.append({"stage": parameters.get("o"), "code": code, "hsc": status.get("hsc")})
        self.auth_trace = self.auth_trace[-8:]
        if type(code) is int and code != 0:
            # Never retain response bodies, Apple message strings, tokens or request fields.
            raise AppleAuthenticationError(parameters.get("o"), code)
        # New Apple responses can omit ec/au and signal trusted-device 2FA with hsc=409.
        if code is None and status.get("hsc") in (200, 409):
            status["ec"] = 0
        if parameters.get("o") == "complete" and status.get("hsc") == 409 and not status.get("au"):
            status["au"] = "trustedDeviceSecondaryAuth"
        return response

    async def get_2fa_methods(self):
        try:
            return await super().get_2fa_methods()
        except UnhandledProtocolError:
            # Apple may refuse the SMS listing even when trusted-device verification works.
            if self.login_state == LoginState.REQUIRE_2FA and (self._account_info or {}).get("trusted_device_2fa"):
                return [AsyncTrustedDeviceSecondFactor(self)]
            raise
