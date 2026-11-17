"""One FCM connection per account, routing concurrent requests by UUID and device."""
import threading
import os
import time
import uuid

import backend
import requests


def network_call(call, *, retry_read=False):
    """Retry once; read timeouts require an explicitly repeatable operation."""
    for attempt in range(2):
        try:
            return call()
        except requests.exceptions.RequestException as error:
            message = str(error).lower()
            retryable = (retry_read and isinstance(error, requests.exceptions.ReadTimeout)) or isinstance(error, (requests.exceptions.ConnectTimeout,
                requests.exceptions.ProxyError)) or (
                isinstance(error, requests.exceptions.SSLError)
                and any(marker in message for marker in ('eof', 'reset', 'closed'))
                and 'certificate' not in message)
            if attempt or not retryable:
                raise
            time.sleep(1)


class RateLimited(backend.PublicError):
    pass


class PushDisconnected(backend.PublicError):
    """The account receiver must be recreated before another query."""
    pass


class LocationTransport:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = None
        self.expires = 0
        self.cooldown = 0
        self.auth_error_until = 0
        self.network_error_until = 0
        self.send_lock = threading.Lock()
        self.query_spacing = max(0, float(os.environ.get('GOOGLEFINDMY_QUERY_SPACING', '0')))
        self.next_send = 0

    def _send_slot(self):
        # Pace starts only; responses still run concurrently (up to 10).
        with self.send_lock:
            if self.cooldown > time.monotonic():
                raise RateLimited('Google 请求限流，查询暂停 15 分钟后重试')
            delay = self.next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if self.cooldown > time.monotonic():
                raise RateLimited('Google 请求限流，查询暂停 15 分钟后重试')
            self.next_send = time.monotonic() + self.query_spacing

    def _token(self, rejected=None):
        from Auth.adm_token_retrieval import get_adm_token
        from Auth.username_provider import get_username
        with self.lock:
            if self.cooldown > time.monotonic():
                raise RateLimited('Google 请求限流，查询暂停 15 分钟后重试')
            if self.auth_error_until > time.monotonic():
                raise backend.NeedsAuth('Google 查询授权暂不可用，请检查账号授权')
            if self.network_error_until > time.monotonic():
                raise backend.PublicError('Google 令牌请求网络暂不可用，稍后重试；不代表账号授权失效')
            if not self.token or time.monotonic() >= self.expires or rejected == self.token:
                try:
                    self.token = network_call(lambda: get_adm_token(get_username()), retry_read=True)
                    self.expires = time.monotonic() + 60
                except requests.exceptions.RequestException as error:
                    self.network_error_until = time.monotonic() + 10
                    raise backend.PublicError(f'Google 令牌请求网络失败（{type(error).__name__}），稍后重试；不代表账号授权失效') from None
                except backend.NeedsAuth:
                    self.auth_error_until = time.monotonic() + 60
                    raise
                except Exception as error:
                    # Upstream also raises KeyError for a missing Auth field in
                    # temporary/error responses. This is not proof of revoked auth.
                    self.network_error_until = time.monotonic() + 10
                    raise backend.PublicError(f'Google 令牌响应异常（{type(error).__name__}），稍后重试；尚不能判定账号授权失效') from None
            return self.token

    def send(self, scope, payload):
        import requests
        token = self._token()
        for attempt in range(2):
            def post():
                self._send_slot()
                return requests.post('https://android.googleapis.com/nova/' + scope,
                    headers={'Authorization': 'Bearer ' + token,
                             'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                             'Accept-Language': 'en-US', 'User-Agent': 'fmd/20006320; gzip'},
                    data=bytes.fromhex(payload), timeout=30)
            try:
                # This transport is used only for location retrieval. Retry the
                # identical payload/UUID; LocationSession accepts one matching
                # FCM result and ignores duplicate responses.
                response = network_call(post, retry_read=True)
            except requests.exceptions.RequestException as error:
                raise backend.PublicError(f'Google 位置请求网络失败（{type(error).__name__}），稍后重试；不代表账号授权失效') from None
            if response.status_code == 429:
                with self.lock:
                    self.cooldown = time.monotonic() + 900
                raise RateLimited('Google 请求限流，查询暂停 15 分钟后重试')
            if response.status_code in (401, 403):
                if attempt:
                    with self.lock:
                        self.auth_error_until = time.monotonic() + 60
                    raise backend.NeedsAuth('Google 查询授权被拒绝，请检查账号授权')
                token = self._token(rejected=token)
                continue
            if response.status_code != 200:
                raise backend.PublicError(f'Google 设备请求被拒绝（HTTP {response.status_code}）')
            return response.content.hex()


class LocationSession:
    def __init__(self, store, receiver=None, transport=None):
        from Auth.fcm_receiver import FcmReceiver
        self.store = store
        self.receiver = receiver if receiver is not None else FcmReceiver()
        self.transport = transport if transport is not None else LocationTransport()
        self.pending = {}
        self.lock = threading.Lock()
        self.response_timeout = 60

    def start(self):
        try:
            self.receiver.register_for_location_updates(self.callback)
        except TimeoutError:
            raise backend.PublicError('Google 推送初始化超时（FCM 签到/注册阶段）；请检查服务端 Google 网络出口') from None
        self.wait_connected()

    def wait_connected(self):
        deadline = time.monotonic() + 60
        while True:
            pc = self.receiver.pc
            state = getattr(getattr(pc, 'run_state', None), 'name', '')
            tasks = getattr(pc, 'tasks', ()) or ()
            # The outer process can remain alive after the upstream listener
            # terminates. Do not spend another minute waiting on dead tasks.
            if state in ('STOPPING', 'STOPPED') or any(t.done() for t in tasks):
                raise PushDisconnected('Google 推送监听已停止，正在重建连接')
            if pc.is_started():
                return
            if time.monotonic() >= deadline:
                raise PushDisconnected('Google 推送通道未在 60 秒内完成连接登录；已请求重建连接，稍后自动重试')
            time.sleep(.1)

    def callback(self, payload):
        from ProtoDecoders.decoder import parse_device_update_protobuf
        try:
            update = parse_device_update_protobuf(payload)
            request_id = update.fcmMetadata.requestUuid
            with self.lock:
                target = self.pending.get(request_id)
                if not target or target['done'].is_set():
                    return
                # A UUID alone must never route another device's ciphertext.
                if backend.canonical(update.deviceMetadata) != target['canonical_id']:
                    return
                target['result'] = update
                target['done'].set()
        except Exception:
            pass

    def locate(self, device_id):
        from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
        from NovaApi.scopes import NOVA_ACTION_API_SCOPE
        dev = self.store.get('device:' + device_id)
        if not dev or dev['registration'] != 'registered' or dev.get('delete_state'):
            raise backend.PublicError('设备尚未注册或正在删除，不能查询')
        self.wait_connected()
        request_id = str(uuid.uuid4())
        target = {'done': threading.Event(), 'canonical_id': dev['canonical_id']}
        with self.lock:
            if any(t['canonical_id'] == dev['canonical_id'] for t in self.pending.values()):
                raise backend.PublicError('相同设备已有查询正在运行')
            self.pending[request_id] = target
        try:
            token = self.receiver.credentials['fcm']['registration']['token']
            self.transport.send(NOVA_ACTION_API_SCOPE,
                                create_location_request(dev['canonical_id'], token, request_id))
            if not target['done'].wait(self.response_timeout):
                if not self.receiver.pc.is_started():
                    raise PushDisconnected('等待位置期间 Google 推送连接中断；已请求重建连接，稍后自动重试')
                raise backend.PublicError('Google 推送连接在等待期限内未返回匹配位置；这不等同于返回 0 条位置')
            return {'points': backend.decrypt_update(target['result'], bytes.fromhex(dev['eik_hex']))}
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        if self.callback in self.receiver.location_update_callbacks:
            self.receiver.location_update_callbacks.remove(self.callback)
        self.receiver.stop_listening()
