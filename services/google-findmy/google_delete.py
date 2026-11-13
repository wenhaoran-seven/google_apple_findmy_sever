"""DeleteBleDevice wire contract confirmed by the upstream project author.

https://github.com/leonboe1/GoogleFindMyTools/issues/41#issuecomment-3015218104
message DeleteBleDeviceRequest { CanonicId deviceToBeDeleted = 1; }
"""
import uuid
import bootstrap
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
from ProtoDecoders import DeviceUpdate_pb2


def deletion_payload(canonical_id):
    # This service only accepts the canonical UUID it reconciled from Google.
    uuid.UUID(canonical_id)
    pool = descriptor_pool.Default()
    name = 'localfindhub.DeleteBleDeviceRequest'
    try:
        descriptor = pool.FindMessageTypeByName(name)
    except KeyError:
        file = descriptor_pb2.FileDescriptorProto(
            name='localfindhub/DeleteBleDevice.proto', package='localfindhub', syntax='proto3')
        file.dependency.append(DeviceUpdate_pb2.DESCRIPTOR.name)
        message = file.message_type.add(name='DeleteBleDeviceRequest')
        message.field.add(name='deviceToBeDeleted', number=1,
                          label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
                          type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
                          type_name='.'+DeviceUpdate_pb2.CanonicId.DESCRIPTOR.full_name)
        pool.AddSerializedFile(file.SerializeToString())
        descriptor = pool.FindMessageTypeByName(name)
    request = message_factory.GetMessageClass(descriptor)()
    request.deviceToBeDeleted.id = canonical_id
    return request.SerializeToString()
