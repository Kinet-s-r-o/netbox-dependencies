import django_tables2 as tables

from netbox.tables import NetBoxTable, columns

from .models import CustomMap, Dependency, PeerLink


class DependencyTable(NetBoxTable):
    upstream = tables.Column(linkify=True, verbose_name='Upstream device')
    downstream = tables.Column(linkify=True, verbose_name='Downstream device')
    dependency_type = columns.ChoiceFieldColumn(verbose_name='Type')
    medium = columns.ChoiceFieldColumn(verbose_name='Medium')
    redundancy_group = tables.Column(verbose_name='Redundancy group')
    description = tables.Column()
    tags = columns.TagColumn(url_name='plugins:netbox_dependencies:dependency_list')

    class Meta(NetBoxTable.Meta):
        model = Dependency
        fields = (
            'pk', 'id', 'upstream', 'downstream', 'dependency_type', 'medium',
            'redundancy_group', 'description', 'tags', 'created', 'last_updated', 'actions',
        )
        default_columns = (
            'pk', 'upstream', 'downstream', 'dependency_type', 'medium',
            'redundancy_group', 'description',
        )


class CustomMapTable(NetBoxTable):
    name = tables.Column(linkify=True)
    description = tables.Column()
    owner = tables.Column()
    device_count = tables.Column(verbose_name='Devices')
    tags = columns.TagColumn(url_name='plugins:netbox_dependencies:custommap_list')

    class Meta(NetBoxTable.Meta):
        model = CustomMap
        fields = ('pk', 'id', 'name', 'description', 'owner', 'device_count', 'tags', 'created', 'last_updated', 'actions')
        default_columns = ('pk', 'name', 'description', 'owner', 'device_count')


class PeerLinkTable(NetBoxTable):
    device_a = tables.Column(linkify=True, verbose_name='Device A')
    device_b = tables.Column(linkify=True, verbose_name='Device B')
    medium = columns.ChoiceFieldColumn(verbose_name='Medium')
    description = tables.Column()
    tags = columns.TagColumn(url_name='plugins:netbox_dependencies:peerlink_list')

    class Meta(NetBoxTable.Meta):
        model = PeerLink
        fields = ('pk', 'id', 'device_a', 'device_b', 'medium', 'description', 'tags', 'created', 'last_updated', 'actions')
        default_columns = ('pk', 'device_a', 'device_b', 'medium', 'description')
