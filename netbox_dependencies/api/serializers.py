from dcim.api.serializers import DeviceSerializer
from netbox.api.serializers import NetBoxModelSerializer

from ..models import Dependency


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
