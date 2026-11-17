"""Adapters for GoogleFindMyTools (GPL-3.0), pinned by UPSTREAM_COMMIT.

Uses upstream crypto/protobufs; replaces interactive flows and unbounded waits.
"""
import base64
import hashlib
import secrets
import threading
import time
import uuid

import bootstrap
from store import Store
from renewal_policy import excluded_eids, is_excluded


class PublicError(RuntimeError):
    """A fixed, user-safe message without upstream response bodies."""
    pass


class NeedsAuth(PublicError):
    pass


def setup(store):
    bootstrap.configure_cache(store)
    bootstrap.configure_network()
    bootstrap.fix_fcm_loop()
    from auth_recovery import install
    install(store, PublicError, NeedsAuth)
    if not all(store.get('auth:'+k) for k in ('username', 'aas_token', 'owner_key', 'fcm_credentials')):
        raise NeedsAuth('请先在本机完成 Google 登录和加密数据授权，再导入授权。')
    # Ensure no headless operation can unexpectedly launch Chrome or await input.
    import Auth.auth_flow as flow
    import KeyBackup.shared_key_flow as shared
    def blocked():
        raise NeedsAuth('授权已失效，请在本机重新授权。')
    flow.request_oauth_account_token_flow = blocked
    shared.request_shared_key_flow = blocked
    import SpotApi.spot_request as spot
    import NovaApi.nova_request as nova
    spot.spot_request = spot_request
    nova.nova_request = nova_request


def spot_request(scope, payload):
    import httpx
    from Auth.spot_token_retrieval import get_spot_token
    from Auth.username_provider import get_username
    from SpotApi.grpc_parser import GrpcParser
    from spot_transport import capture_grpc_trailers, decode_response
    capture_grpc_trailers()
    trailers = {}
    with httpx.Client(http2=True, timeout=30) as client:
        r = client.post('https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/'+scope,
                        headers={'Authorization': 'Bearer '+get_spot_token(get_username()),
                                 'Content-Type': 'application/grpc', 'Te': 'trailers',
                                 'User-Agent': 'com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT'},
                        content=GrpcParser.construct_grpc(payload), extensions={'findhub_grpc_trailers':trailers})
    return decode_response(r, trailers, scope, PublicError)


def nova_request(scope, hex_payload):
    import requests
    from Auth.adm_token_retrieval import get_adm_token
    from Auth.username_provider import get_username
    r = requests.post('https://android.googleapis.com/nova/'+scope,
                      headers={'Authorization': 'Bearer '+get_adm_token(get_username()),
                               'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                               'Accept-Language': 'en-US', 'User-Agent': 'fmd/20006320; gzip'},
                      data=bytes.fromhex(hex_payload), timeout=30)
    if r.status_code != 200:
        raise PublicError(f'Google 设备请求被拒绝（HTTP {r.status_code}）')
    return r.content.hex()


def device_list():
    from NovaApi.ListDevices.nbe_list_devices import request_device_list
    from ProtoDecoders.decoder import parse_device_list_protobuf
    return parse_device_list_protobuf(request_device_list())


def canonical(meta):
    info = meta.identifierInformation
    ids = info.canonicIds.canonicId or info.phoneInformation.canonicIds.canonicId
    return ids[0].id if ids else None


def identity_key(meta, store):
    from KeyBackup.cloud_key_decryptor import decrypt_eik
    from SpotApi.CreateBleDevice.util import flip_bits
    from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id
    reg = meta.information.deviceRegistration
    encrypted = flip_bits(reg.encryptedUserSecrets.encryptedIdentityKey, reg.fastPairModelId == mcu_fast_pair_model_id)
    return decrypt_eik(bytes.fromhex(store.get('auth:owner_key')), encrypted)


def sync(store):
    from FMDNCrypto.eid_generator import generate_eid
    from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id
    result = device_list()
    local = store.all('device:')
    rows = []
    for meta in result.deviceMetadata:
        cid = canonical(meta)
        if not cid:
            continue
        reg = meta.information.deviceRegistration
        eik = None
        if reg.fastPairModelId == mcu_fast_pair_model_id:
            eik = identity_key(meta, store).hex()
        match = next((x for x in local if x.get('canonical_id') == cid or (eik and x.get('eik_hex') == eik)), None)
        if match:
            match.update(canonical_id=cid, registration='registered', pair_date=reg.pairDate)
            store.put('device:'+match['id'], match)
        rows.append({'canonical_id': cid, 'name': meta.userDefinedDeviceName,
                     'custom_tracker': reg.fastPairModelId == mcu_fast_pair_model_id,
                     'local_device_id': match['id'] if match else None})
    store.put('cloud_devices', rows)
    store.put('last_sync', time.time())
    return {'devices': rows}


def build_registration(device, owner_key, owner_version, pair_date):
    from ProtoDecoders.DeviceUpdate_pb2 import RegisterBleDeviceRequest, SpotDeviceType
    from FMDNCrypto.key_derivation import FMDNOwnerOperations
    from KeyBackup.cloud_key_decryptor import encrypt_aes_gcm
    from SpotApi.CreateBleDevice.util import flip_bits
    from SpotApi.CreateBleDevice.config import mcu_fast_pair_model_id, max_truncated_eid_seconds_server
    from SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import get_next_eids
    eik = bytes.fromhex(device['eik_hex'])
    req = RegisterBleDeviceRequest()
    req.fastPairModelId = mcu_fast_pair_model_id
    req.description.userDefinedName = device['name']
    req.description.deviceType = SpotDeviceType.DEVICE_TYPE_BEACON
    req.capabilities.isAdvertising = True
    req.capabilities.trackableComponents = req.capabilities.capableComponents = 1
    e2e = req.e2eePublicKeyRegistration
    e2e.rotationExponent, e2e.pairingDate = 10, pair_date
    secret = e2e.encryptedUserSecrets
    secret.encryptedIdentityKey = flip_bits(encrypt_aes_gcm(owner_key, eik), True)
    secret.encryptedAccountKey, secret.encryptedSha256AccountKeyPublicAddress = secrets.token_bytes(44), secrets.token_bytes(60)
    secret.ownerKeyVersion = owner_version
    secret.creationDate.seconds = pair_date
    e2e.publicKeyIdList.publicKeyIdInfo.extend(get_next_eids(eik, pair_date, pair_date, max_truncated_eid_seconds_server))
    req.manufacturerName, req.modelName = 'GoogleFindMyTools', 'µC'
    keys = FMDNOwnerOperations()
    keys.generate_keys(eik)
    req.ringKey, req.recoveryKey, req.unwantedTrackingKey = keys.ringing_key, keys.recovery_key, keys.tracking_key
    return req


def register(store, device_id):
    if is_excluded(store.get('device:'+device_id) or {}, excluded_eids(store)):
        raise PublicError('此 EID 已暂停续报测试，注册也会提交标识，因此暂不执行注册。')
    sync(store)  # Reconcile before sending anything, including after a lost response.
    dev = store.get('device:'+device_id)
    if dev['registration'] == 'registered':
        return {'message': '设备已注册，无需重复注册'}
    if dev['registration'] == 'unknown':
        raise PublicError('上次注册结果未确认。同步列表仍未找到匹配项，请先核查 Google 账号，避免重复注册。')
    from SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import get_eid_info
    version = get_eid_info().encryptedOwnerKeyAndMetadata.ownerKeyVersion
    pair_date = int(time.time())
    req = build_registration(dev, bytes.fromhex(store.get('auth:owner_key')), version, pair_date)
    dev.update(registration='unknown', pair_date=pair_date)
    store.put('device:'+device_id, dev)  # Persist before the network side effect.
    spot_request('CreateBleDevice', req.SerializeToString())
    sync(store)
    confirmed = store.get('device:'+device_id)
    if confirmed['registration'] != 'registered':
        raise PublicError('Google 已接收注册请求，但设备列表尚未确认。请稍后同步列表；不要重新生成密钥。')
    confirmed['announced_at'] = time.time()
    store.put('device:'+device_id, confirmed)
    return {'message': '已在 Google 设备列表确认注册，可烧录广播 EID'}


def renew(store):
    import httpx
    import requests
    from ProtoDecoders.DeviceUpdate_pb2 import UploadPrecomputedPublicKeyIdsRequest
    from SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import get_next_eids
    from SpotApi.CreateBleDevice.config import max_truncated_eid_seconds_server
    exclusions = excluded_eids(store)
    candidates = [d for d in store.all('device:') if d['registration'] == 'registered' and not d.get('delete_state')]
    devices = [d for d in candidates if not is_excluded(d, exclusions)]
    # A large account-wide payload can exceed the proxy/Google response timeout.
    # Checkpoint only acknowledged batches; a later attempt skips fresh devices.
    started = time.time()
    deadline = time.monotonic()+90
    pending = sorted((d for d in devices if d.get('announced_at', 0) <= started-86400),
                     key=lambda d: d.get('announced_at', 0))
    completed = len(devices)-len(pending)
    state = {**store.get('renewal', {}), 'last_attempt': started,
             'total_devices': len(devices), 'completed_devices': completed,
             'pending_devices': len(pending), 'batch_size': 10}
    store.put('renewal', state)
    for offset in range(0, len(pending), 10):
        # Leave room for token acquisition (30s) and upload (30s) within the
        # parent's 180s job limit. Completed batches survive a later failure.
        if time.monotonic() >= deadline:
            raise PublicError(f'续报已确认 {completed}/{len(devices)} 台，本轮时间预算已用完，剩余设备稍后自动继续。')
        batch = pending[offset:offset+10]
        req = UploadPrecomputedPublicKeyIdsRequest()
        for d in batch:
            row = req.deviceEids.add()
            row.canonicId.id, row.pairDate = d['canonical_id'], d['pair_date']
            row.clientList.publicKeyIdInfo.extend(get_next_eids(bytes.fromhex(d['eik_hex']), d['pair_date'], int(time.time())-10800, max_truncated_eid_seconds_server))
        try:
            spot_request('UploadPrecomputedPublicKeyIds', req.SerializeToString())
        except (httpx.TransportError, requests.exceptions.RequestException) as error:
            raise PublicError(f'续报网络请求失败（{type(error).__name__}），已确认 {completed}/{len(devices)} 台；剩余设备稍后自动重试。') from None
        for d in batch:
            current = store.get('device:'+d['id'])
            if current:
                current['announced_at'] = time.time()
                store.put('device:'+d['id'], current)
        completed += len(batch)
        state.update(completed_devices=completed, pending_devices=len(devices)-completed)
        store.put('renewal', state)
    store.put('renewal', {**state, 'last_success': time.time(), 'error': None,
                          'failures': 0, 'retry_after': 0})
    return {'renewed_devices': len(devices), 'excluded_devices': len(candidates)-len(devices)}


def delete_device(store, device_id):
    from google_delete import deletion_payload
    dev = store.get('device:'+device_id)
    if not dev or not dev.get('delete_state'):
        raise PublicError('删除任务尚未确认，请从设备列表重新操作')

    def matching(rows):
        # sync() reconciles both canonical ID and decrypted identity key.
        current = store.get('device:'+device_id)
        return [r for r in rows if r.get('local_device_id') == device_id
                or (current.get('canonical_id') and r['canonical_id'] == current['canonical_id'])]

    try:
        found = matching(sync(store)['devices'])
    except Exception:
        raise PublicError('无法读取 Google 设备列表，尚未发送注销请求；设备与历史已保留，请恢复网络后重试删除') from None
    if found:
        if len(found) != 1 or not found[0]['custom_tracker']:
            raise PublicError('Google 设备身份不唯一或不是本服务的标签，已停止删除')
        dev = store.get('device:'+device_id)
        payload = deletion_payload(found[0]['canonical_id'])
        dev.update(delete_sent_at=time.time())
        store.put('device:'+device_id, dev)
        try:
            spot_request('DeleteBleDevice', payload)
        except Exception:
            # A lost reply or empty gRPC response is not evidence of success.
            # Only a subsequent authenticated list can authorize local removal.
            pass
    # Check twice, including when the device had already been removed in the
    # official app or this is a retry after a lost response.
    absent = 0
    for delay in (0, 2, 3):
        if delay:
            time.sleep(delay)
        try:
            exists = bool(matching(sync(store)['devices']))
        except Exception:
            raise PublicError('Google 注销结果暂时无法核实；设备与历史已保留、查询已暂停，请恢复网络后重试删除') from None
        absent = 0 if exists else absent+1
        if absent >= 2:
            store.archive_deleted_device(device_id)
            return {'message': '已确认 Google 列表中无此设备；本服务设备已移除，历史与密钥已加密归档',
                    'cloud_absence_confirmed': True}
    raise PublicError('Google 列表中仍存在该设备，注销尚未确认；本服务记录已保留、查询已暂停，请稍后重试删除')


def decrypt_update(update, eik, custom=True):
    from ProtoDecoders import Common_pb2, DeviceUpdate_pb2
    from FMDNCrypto.foreign_tracker_cryptor import decrypt
    from KeyBackup.cloud_key_decryptor import decrypt_aes_gcm
    info = update.deviceMetadata.information.locationInformation.reports.recentLocationAndNetworkLocations
    from response_fields import location_collection_unknown_11
    unknown_11 = location_collection_unknown_11(info)
    received_at = time.time()
    locations = list(zip(info.networkLocations, info.networkLocationTimestamps))
    if info.HasField('recentLocation'):
        locations.append((info.recentLocation, info.recentLocationTimestamp))
    points = []
    for loc, stamp in locations:
        # Response-level metadata, not a proven state at the location timestamp.
        point = {'timestamp': stamp.seconds, 'status': int(loc.status),
                 'unknown_11': unknown_11, 'unknown_11_received_at': received_at}
        if loc.status == Common_pb2.Status.SEMANTIC:
            point.update(name=loc.semanticLocation.locationName, latitude=None, longitude=None, accuracy_m=None)
        else:
            encrypted = loc.geoLocation.encryptedReport
            if encrypted.publicKeyRandom:
                plain = decrypt(eik, encrypted.encryptedLocation, encrypted.publicKeyRandom, 0 if custom else loc.geoLocation.deviceTimeOffset)
            else:
                plain = decrypt_aes_gcm(hashlib.sha256(eik).digest(), encrypted.encryptedLocation)
            decoded = DeviceUpdate_pb2.Location()
            decoded.ParseFromString(plain)
            lat, lon = decoded.latitude / 1e7, decoded.longitude / 1e7
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError('Invalid decrypted coordinate')
            point.update(latitude=lat, longitude=lon, altitude=decoded.altitude,
                         accuracy_m=loc.geoLocation.accuracy, is_own_report=encrypted.isOwnReport)
        points.append(point)
    return points


def locate(store, device_id):
    from Auth.fcm_receiver import FcmReceiver
    from NovaApi.ExecuteAction.LocateTracker.location_request import create_location_request
    from NovaApi.scopes import NOVA_ACTION_API_SCOPE
    from ProtoDecoders.decoder import parse_device_update_protobuf
    dev = store.get('device:'+device_id)
    if dev['registration'] != 'registered':
        raise PublicError('请先注册设备')
    req_id = str(uuid.uuid4())
    done, result = threading.Event(), []
    def callback(payload):
        try:
            update = parse_device_update_protobuf(payload)
            if update.fcmMetadata.requestUuid == req_id and canonical(update.deviceMetadata) in (None, dev['canonical_id']):
                result.append(update)
                done.set()
        except Exception:
            pass
    receiver = FcmReceiver()
    try:
        try:
            token = receiver.register_for_location_updates(callback)
        except TimeoutError:
            raise PublicError('Google 推送初始化超时（FCM 签到/注册阶段）；请检查服务端 Google 网络出口。这不代表设备未注册或返回 0 条位置。') from None
        # start() only schedules the MCS listener. Wait for its successful
        # LoginResponse before asking Google to deliver a location update.
        deadline = time.monotonic()+20
        while not receiver.pc.is_started():
            if time.monotonic() >= deadline:
                raise PublicError('Google 推送通道未在 20 秒内完成连接登录；请检查 Google 网络及 mtalk.google.com:5228。位置查询尚未发送。')
            time.sleep(0.1)
        nova_request(NOVA_ACTION_API_SCOPE, create_location_request(dev['canonical_id'], token, req_id))
        if not done.wait(60):
            if not receiver.pc.is_started():
                raise PublicError('Google 推送接收器已停止或未完成连接；请检查服务端推送连接与解析状态')
            raise PublicError('Google 推送连接正常，但 60 秒内未收到匹配的位置响应；这不等同于返回 0 条位置')
        points = decrypt_update(result[-1], bytes.fromhex(dev['eik_hex']))
        return {'points': points}
    finally:
        if callback in receiver.location_update_callbacks:
            receiver.location_update_callbacks.remove(callback)


def run(operation, device_id=None):
    store = Store()
    setup(store)
    if operation == 'auth-check':
        # Reuse the stored Android ID; do not open a second FCM receiver.
        import gpsoauth
        response = gpsoauth.perform_oauth(store.get('auth:username'), store.get('auth:aas_token'),
            store.get('auth:fcm_credentials')['gcm']['android_id'],
            service='oauth2:https://www.googleapis.com/auth/android_device_manager',
            app='com.google.android.apps.adm', client_sig='38918a453d07199354f8b19af05ec6562ced5788')
        return {'checked': bool(response.get('Auth'))}
    if operation == 'sync': return sync(store)
    if operation == 'register': return register(store, device_id)
    if operation == 'renew': return renew(store)
    if operation == 'locate': return locate(store, device_id)
    if operation == 'delete': return delete_device(store, device_id)
    raise ValueError('Unknown operation')
