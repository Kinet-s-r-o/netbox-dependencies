from dcim.api.serializers import DeviceSerializer
from netbox.api.serializers import NetBoxModelSerializer

from users.api.serializers import UserSerializer

from ..models import CustomMap, Dependency, PeerLink


class DependencySerializer(NetBoxModelSerializer):
    upstream = DeviceSerializer(nested=True)
    downstream = DeviceSerializer(nested=True)

    class Meta:
        model = Dependency
        fields = (
            'id', 'url', 'display', 'upstream', 'downstream', 'dependency_type', 'medium',
            'redundancy_group', 'description', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'upstream', 'downstream')


class CustomMapSerializer(NetBoxModelSerializer):
    owner = UserSerializer(nested=True, read_only=True, allow_null=True)
    devices = DeviceSerializer(nested=True, many=True, required=False)

    class Meta:
        model = CustomMap
        fields = (
            'id', 'url', 'display', 'name', 'description', 'owner', 'devices', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name')


class PeerLinkSerializer(NetBoxModelSerializer):
    device_a = DeviceSerializer(nested=True)
    device_b = DeviceSerializer(nested=True)

    class Meta:
        model = PeerLink
        fields = (
            'id', 'url', 'display', 'device_a', 'device_b', 'medium', 'description', 'tags', 'custom_fields',
            'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'device_a', 'device_b')
