"""Link between the graph algorithm and the NetBox models."""
from dcim.models import Device

from .graph import DOWN, compute_impact
from .models import Dependency


def load_edges():
    return list(Dependency.objects.values_list('upstream_id', 'downstream_id'))


def get_impact(failed_ids, edges=None):
    """Returns (rows, summary): rows = list of {device, state, depth} ordered
    by state, depth and name; summary = counts per state."""
    failed_ids = {int(i) for i in failed_ids}
    result = compute_impact(load_edges() if edges is None else edges, failed_ids)
    devices = Device.objects.filter(pk__in=result.keys()).select_related(
        'site', 'role', 'tenant', 'device_type'
    )
    rows = [
        {'device': d, 'state': result[d.pk].state, 'depth': result[d.pk].depth}
        for d in devices
    ]
    rows.sort(key=lambda r: (r['depth'], r['device'].name or ''))
    summary = {DOWN: len(rows)}
    return rows, summary
