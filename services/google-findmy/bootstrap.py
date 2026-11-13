import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPSTREAM = Path(os.environ.get('GOOGLEFINDMY_UPSTREAM', ROOT / 'upstream'))
if not UPSTREAM.exists():
    UPSTREAM = ROOT.parent / 'vendor' / 'GoogleFindMyTools'
sys.path.insert(0, str(UPSTREAM))


def configure_cache(store):
    import Auth.token_cache as cache
    cache.get_cached_value = lambda name: store.get('auth:'+name)
    cache.set_cached_value = lambda name, value: store.put('auth:'+name, value)
    def get_or_set(name, generator):
        value = cache.get_cached_value(name)
        if value is None:
            value = generator()
            cache.set_cached_value(name, value)
        return value
    cache.get_cached_value_or_set = get_or_set


def configure_network():
    """HTTP libraries use explicit proxy; MCS retains end-to-end TLS on port 5228."""
    import requests
    original = requests.Session.request
    if not getattr(original, '_bounded', False):
        def request(self, *args, **kwargs):
            kwargs.setdefault('timeout', 30)
            return original(self, *args, **kwargs)
        request._bounded = True
        requests.Session.request = request
    proxy = os.environ.get('GOOGLEFINDMY_PROXY')
    if not proxy:
        return
    os.environ['HTTPS_PROXY'] = proxy
    os.environ['HTTP_PROXY'] = proxy
    os.environ['NO_PROXY'] = '127.0.0.1,localhost'
    import aiohttp
    from Auth.firebase_messaging import fcmregister
    fcmregister.ClientSession = lambda *a, **k: aiohttp.ClientSession(*a, **{**k, 'trust_env': True})
    from Auth.firebase_messaging.fcmpushclient import FcmPushClient
    async def connect(self, host, port, ssl_context):
        import asyncio
        from python_socks.async_.asyncio import Proxy
        sock = await Proxy.from_url(proxy).connect(dest_host=host, dest_port=port, timeout=10)
        try:
            return await asyncio.wait_for(asyncio.open_connection(
                sock=sock, ssl=ssl_context, server_hostname=host), timeout=12)
        except BaseException:
            sock.close()
            raise
    FcmPushClient._open_connection = connect


def fix_fcm_loop():
    """Register and listen on the same asyncio loop; upstream crosses closed loops."""
    from fcm_compat import fix_push_base64
    fix_push_base64()
    import asyncio
    import threading
    from Auth.fcm_receiver import FcmReceiver
    def start(self):
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_event_loop_in_thread, daemon=True)
        self._loop_thread.start()
        future = asyncio.run_coroutine_threadsafe(self._register_for_fcm(), self._loop)
        try:
            future.result(timeout=50)
        except TimeoutError:
            future.cancel()
            raise
        asyncio.run_coroutine_threadsafe(self.pc.start(), self._loop)
        self._listening = True
        return self.credentials['gcm']['android_id']
    FcmReceiver._start_listener_in_background = start
