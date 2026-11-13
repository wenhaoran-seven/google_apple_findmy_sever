"""Explicit HTTPS origins for the authenticated reverse-proxy deployment."""
import json
import os
import re
from urllib.parse import urlsplit


def validate_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r'https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?', value):
        raise ValueError('Public origin must be an exact HTTPS origin')
    parsed = urlsplit(value)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid origin port')
    return value


def load_origins():
    values = json.loads(os.environ.get('FINDMY_PUBLIC_ORIGINS', '[]'))
    if not isinstance(values, list):
        raise ValueError('FINDMY_PUBLIC_ORIGINS must be a JSON list')
    return frozenset(validate_origin(value) for value in values)


PUBLIC_ORIGINS = load_origins()
PUBLIC_MAP_ORIGIN = os.environ.get('FINDMY_MAP_ORIGIN', '')
if PUBLIC_MAP_ORIGIN:
    validate_origin(PUBLIC_MAP_ORIGIN)
    if PUBLIC_MAP_ORIGIN in PUBLIC_ORIGINS:
        raise ValueError('The third-party map must have a separate origin')
