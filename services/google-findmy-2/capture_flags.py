"""Temporary, bounded capture of matching responses, encrypted in the service vault."""
import time
import uuid


def capture(store, device, payload):
    try:
        config = store.get('diagnostic:flags-aa-config', {})
        if time.time() > config.get('expires_at', 0) or device['id'] not in config.get('device_ids', []):
            return
        prefix = 'diagnostic:flags-aa:' + config['run_id'] + ':'
        if len(store.names(prefix)) >= 40:
            return
        store.put(prefix + str(uuid.uuid4()), {
            'received_at': time.time(), 'device_id': device['id'], 'sn': device['sn'],
            'payload_hex': payload,
        })
    except Exception:
        # Diagnostics must not interrupt the normal position query.
        return
