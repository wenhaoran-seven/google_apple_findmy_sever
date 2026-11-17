"""Recover token requests without storing passwords or replaying device writes."""
import threading
import time
import requests


def install(store, public_error, needs_auth):
    import gpsoauth
    original = gpsoauth.perform_oauth
    if getattr(original, '_findmy_recovery', False) is True:
        return
    lock = threading.Lock()

    def oauth(*args, **kwargs):
        with lock:
            state = store.get('auth_recovery', {})
            if state.get('state') == 'reauthorization_required':
                raise needs_auth('Google 已要求重新验证，请在账号管理中完成授权')
            if state.get('retry_at', 0) > time.time():
                raise public_error('Google 令牌服务暂不可用，正在等待自动重试；无需重新登录')
            for attempt in range(2):
                try:
                    response = original(*args, **kwargs)
                except requests.exceptions.RequestException:
                    if not attempt:
                        time.sleep(1)
                        continue
                    response = {}
                if isinstance(response, dict) and response.get('Auth'):
                    store.put('auth_recovery', {'state':'healthy', 'updated':time.time(), 'failures':0})
                    return response
                # Only explicit identity challenges invalidate account access.
                code = response.get('Error') if isinstance(response, dict) else None
                if code in ('BadAuthentication', 'NeedsBrowser', 'CaptchaRequired', 'AccountDeleted', 'AccountDisabled'):
                    store.put('auth_recovery', {'state':'reauthorization_required', 'updated':time.time(),
                        'message':'Google 要求重新登录或验证，请在账号管理中完成授权'})
                    raise needs_auth('Google 要求重新登录或验证，请在账号管理中完成授权')
                if not attempt:
                    time.sleep(1)
            failures = state.get('failures', 0) + 1
            delay = min(900, 30 * 2 ** min(failures-1, 5))
            store.put('auth_recovery', {'state':'retrying', 'updated':time.time(),
                'failures':failures, 'retry_at':time.time()+delay,
                'message':'Google 令牌请求暂不可用，将自动重试；尚不能判定授权失效'})
            raise public_error('Google 令牌请求暂不可用，将自动重试；尚不能判定授权失效')
    oauth._findmy_recovery = True
    gpsoauth.perform_oauth = oauth
