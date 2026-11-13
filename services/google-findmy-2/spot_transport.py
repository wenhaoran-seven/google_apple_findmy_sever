"""Preserve Google gRPC trailers discarded by the current httpcore transport."""
import h2.events
from httpcore._sync.http2 import HTTP2Connection


def capture_grpc_trailers():
    original = HTTP2Connection._read_incoming_data
    if getattr(original, '_findhub_trailer_capture', False):
        return
    def read(self, request):
        events = original(self, request)
        sink = request.extensions.get('findhub_grpc_trailers')
        if sink is not None:
            for event in events:
                if isinstance(event, h2.events.TrailersReceived):
                    # Only the status code is retained; no server messages/tokens.
                    for key, value in event.headers:
                        if key in (b'grpc-status', 'grpc-status'):
                            sink[event.stream_id] = value.decode('ascii') if isinstance(value, bytes) else value
        return events
    read._findhub_trailer_capture = True
    HTTP2Connection._read_incoming_data = read


def decode_response(response, trailers, scope, error_type):
    from SpotApi.grpc_parser import GrpcParser
    code = trailers.get(response.extensions.get('stream_id'), response.headers.get('grpc-status'))
    names = {1:'CANCELLED',2:'UNKNOWN',3:'INVALID_ARGUMENT',4:'DEADLINE_EXCEEDED',5:'NOT_FOUND',
             6:'ALREADY_EXISTS',7:'PERMISSION_DENIED',8:'RESOURCE_EXHAUSTED',9:'FAILED_PRECONDITION',
             10:'ABORTED',11:'OUT_OF_RANGE',12:'UNIMPLEMENTED',13:'INTERNAL',14:'UNAVAILABLE',
             15:'DATA_LOSS',16:'UNAUTHENTICATED'}
    if response.status_code != 200:
        raise error_type(f'Google {scope} 请求被拒绝（HTTP {response.status_code}）')
    if code is not None and code != '0':
        number = int(code) if code.isdecimal() else -1
        raise error_type(f'Google {scope} 请求被拒绝（gRPC {number} {names.get(number,"UNKNOWN_STATUS")}）')
    if scope == 'UploadPrecomputedPublicKeyIds' and code != '0':
        raise error_type('Google 续报未返回明确的 gRPC 成功状态，未确认续报成功')
    if not response.content:
        # Only an explicit RPC success may represent an empty mutation response.
        # Registration still requires a separate cloud-list identity confirmation.
        if code == '0' and scope in ('CreateBleDevice','UploadPrecomputedPublicKeyIds'):
            return b''
        raise error_type(f'Google {scope} 返回空响应，未确认成功（gRPC {code or "未提供"}）')
    if response.content[0] != 0:
        raise error_type(f'Google {scope} 返回不支持的 gRPC 帧（标志 {response.content[0]}）')
    return GrpcParser.extract_grpc_payload(response.content)
