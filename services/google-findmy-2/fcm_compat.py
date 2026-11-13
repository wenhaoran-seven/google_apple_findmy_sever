"""Compatibility with unpadded base64url in Google's Web Push headers."""
import base64


def normalized_base64url(value):
    # The pinned client strips dh=/salt=, but leaves optional ;parameters.
    # RFC base64url permits omitted padding; Python's decoder requires it.
    value = value.split(';', 1)[0].strip()
    raw = value.encode('ascii')
    decoded = base64.b64decode(raw + b'=' * (-len(raw) % 4), altchars=b'-_', validate=True)
    return base64.urlsafe_b64encode(decoded).decode('ascii')


def fix_push_base64():
    from Auth.firebase_messaging.fcmpushclient import FcmPushClient
    original = FcmPushClient._decrypt_raw_data
    if getattr(original, '_base64url_compatible', False):
        return

    def decrypt(credentials, crypto_key, salt, raw_data):
        return original(credentials, normalized_base64url(crypto_key),
                        normalized_base64url(salt), raw_data)

    decrypt._base64url_compatible = True
    FcmPushClient._decrypt_raw_data = staticmethod(decrypt)
