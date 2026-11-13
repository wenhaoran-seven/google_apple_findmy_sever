import hashlib
import secrets
import time
import uuid
import re
import bootstrap
from FMDNCrypto.eid_generator import generate_eid, calculate_r

DEFAULT_CID = "3A105789"

def mask_for_device(device):
    try:
        eik = bytes.fromhex(device["eik_hex"])
        eid = bytes.fromhex(device["eid_hex"])
        if len(eik) != 32 or len(eid) != 20 or generate_eid(eik, 0) != eid:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("EIK 与当前固定 EID 不匹配，无法生成掩码") from None
    r = calculate_r(eik, 0)
    # FMDN hashed flags use the low 160 bits of r, serialized big-endian.
    return f'{hashlib.sha256((r & ((1 << 160)-1)).to_bytes(20, "big")).digest()[-1]:02X}'

def validate_pubkey(pubkey):
    if not isinstance(pubkey, str) or not re.fullmatch(r"[0-9A-Fa-f]{56}", pubkey) or int(pubkey, 16) == 0:
        raise ValueError("请填写 28 字节 / 56 位 HEX 的 Apple 公钥 pubkey（不能全零）")
    return pubkey.upper()

def burn_config(device):
    cid = device.get("cid") or DEFAULT_CID
    mask = mask_for_device(device)
    return {"cid": cid, "pubkey": device.get("pubkey", ""), "mask": mask,
            "command": burn_command(device, cid) if device.get("pubkey") else None}



def address_for_sn(sn):
    # A 12-digit decimal number fits in 40 bits, below the static address bits.
    raw = bytearray(int(sn).to_bytes(6, 'big'))
    raw[0] |= 0xC0
    return ':'.join(f'{b:02X}' for b in raw)


def burn_command(device, cid):
    if not re.fullmatch(r'[0-9A-Fa-f]{8}', cid):
        raise ValueError('CID 必须是设备串口上报的 8 位十六进制字符')
    if not re.fullmatch(r'[1-9][0-9]{11}', device.get('sn', '')):
        raise ValueError('设备尚未分配 SN')
    if not re.fullmatch(r'[0-9A-Fa-f]{40}', device['eid_hex']):
        raise ValueError('EID 长度必须为 20 字节')
    pubkey = validate_pubkey(device.get('pubkey'))
    mask = mask_for_device(device)
    return (f'AT+BURN=sn:{device["sn"]},pubkey:{pubkey},EID:{device["eid_hex"].upper()},'
            f'cid:{cid.upper()},mask:{mask},advT:14,channel:7,txpower:06,hint:00,state:00')


def new_device(name):
    eik = secrets.token_bytes(32)
    eid = generate_eid(eik, 0)
    # Legacy 29-byte advertising data: flags + 16-bit service data.
    adv = bytes.fromhex('0201061916aafe41') + eid + b'\x00'
    address = bytearray(secrets.token_bytes(6))
    address[0] |= 0xC0
    return {'id': str(uuid.uuid4()), 'name': name, 'eik_hex': eik.hex(),
            'eid_hex': eid.hex(), 'advertisement_hex': adv.hex(),
            'static_random_address': ':'.join(f'{b:02X}' for b in address),
            'cid': DEFAULT_CID, 'created_at': time.time(), 'registration': 'draft', 'canonical_id': None,
            'interval_seconds': 300, 'enabled': False, 'last_fetch': None,
            'last_count': None, 'new_count': 0, 'empty_count': 0, 'error': None}


def firmware_header(device):
    raw = bytes.fromhex(device['advertisement_hex'])
    values = ', '.join(f'0x{b:02X}' for b in raw)
    return ('/* GoogleFindMyTools static-EID experimental firmware configuration.\n'
            ' * Register this device in the service before expecting location reports.\n'
            ' * EIK stays on the server. Use legacy ADV_NONCONN_IND, LE 1M, channels 37/38/39.\n'
            ' * No complete Fast Pair / DULT implementation is included. */\n'
            '#pragma once\n#include <stdint.h>\n'
            f'#define GOOGLE_FMDN_SN "{device.get("sn", "")}"\n'
            + (f'#define GOOGLE_FMDN_CID "{device["cid"]}"\n' if device.get('cid') else '') +
            f'#define GOOGLE_FMDN_EID_HEX "{device["eid_hex"]}"\n'
            '#define GOOGLE_FMDN_ADV_INTERVAL_UNITS 3200 /* 2000 ms / 0.625 ms */\n'
            '#define GOOGLE_FMDN_TX_POWER_DBM 4\n'
            f'static const uint8_t google_fmdn_adv_data[29] = {{{values}}};\n')
