"""Unidentified response metadata; deliberately not interpreted as battery."""
from google.protobuf.unknown_fields import UnknownFieldSet


def location_collection_unknown_11(info):
    """Return the raw varint field 11, distinguishing absence from explicit 0.

    This field belongs to RecentLocationAndNetworkLocations, not to an
    individual LocationReport or its encrypted location plaintext.
    """
    # Also work if a future upstream descriptor names this numeric field.
    for descriptor, value in info.ListFields():
        if descriptor.number == 11:
            return int(value) if isinstance(value, int) else None
    result = None
    for field in UnknownFieldSet(info):
        if field.field_number == 11 and field.wire_type == 0:
            result = field.data
    return result
