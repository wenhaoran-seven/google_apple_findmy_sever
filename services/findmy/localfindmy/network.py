import asyncio
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from findmy.util.tls import apple_trust_context
from .transport import apple_proxy_for_url

TARGETS = {
    "GitHub": "https://github.com/malmeloo/FindMy.py",
    "Apple 登录": "https://gsa.apple.com/grandslam/GsService2",
    "Apple 双重验证": "https://gsa.apple.com/auth",
    "iCloud 会话": "https://setup.icloud.com/setup/iosbuddy/loginDelegates",
    "Find My 位置": "https://gateway.icloud.com/findmyservice/v2/fetch",
}


async def check_network():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as session:
        async def probe(label, url):
            started = time.monotonic()
            result = {"name": label, "url": url, "reachable": False}
            try:
                addresses = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    urlparse(url).hostname, 443, type=socket.SOCK_STREAM), timeout=5)
                result["ips"] = sorted({a[4][0] for a in addresses})
                async with session.get(url, ssl=apple_trust_context(), allow_redirects=False,
                                       proxy=apple_proxy_for_url(url)) as response:
                    result.update(reachable=True, http_status=response.status)
            except Exception as exc:
                result["error"] = type(exc).__name__
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            return result
        checks = await asyncio.gather(*(probe(k, v) for k, v in TARGETS.items()))
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": checks,
            "transport_ok": all(c["reachable"] for c in checks),
            "has_service_errors": any(c.get("http_status", 0) >= 500 or c.get("http_status") == 429 for c in checks),
            "note": "使用 FindMy 0.10.2 的 TLS 证书配置直接连接；HTTP 4xx/5xx 只证明链路可达，登录和取位需另行验证。"}
