"""An explicit proxy shared by Apple authentication and location requests."""
import os
from urllib.parse import urlsplit


def is_apple_host(host):
    host = (host or "").lower().rstrip(".")
    return host in ("apple.com", "icloud.com") or host.endswith((".apple.com", ".icloud.com"))


def apple_proxy_for_url(url):
    target = urlsplit(str(url))
    if target.scheme != "https" or not is_apple_host(target.hostname):
        return None
    proxy = os.environ.get("FINDMY_HTTPS_PROXY", "").strip()
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeError("Invalid FINDMY_HTTPS_PROXY configuration")
    return proxy
