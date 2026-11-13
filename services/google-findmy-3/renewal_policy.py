"""Persistent exclusions by broadcast EID, independent of device name/SN."""


def excluded_eids(store):
    return {eid.lower() for eid in store.get('renewal_excluded_eids', [])}


def is_excluded(device, exclusions):
    return device.get('eid_hex', '').lower() in exclusions
