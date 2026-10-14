"""Building the dependency tree for display (roots = devices without an upstream)."""
from collections import defaultdict

from dcim.models import Device

from utilities.paginator import EnhancedPaginator

from .models import Dependency, DeviceStatus


def build_tree(site_id=None, page_number=None, per_page=None):
    """Returns (roots, stats). Each node: {device, state, edge, children, direct, total}.

    A device with several upstream devices appears under each of them (the `edge`
    carries the type and redundancy group for that parent). `total` = number of unique
    devices below the node.

    With `per_page` the roots are paginated: only one page of them is built (stats['page'] is an EnhancedPage,
    otherwise None) and stats['roots'] is the total number of roots."""
    edges = list(Dependency.objects.select_related('upstream', 'downstream'))
    children_of = defaultdict(list)
    parents_count = defaultdict(int)
    device_ids = set()
    for e in edges:
        children_of[e.upstream_id].append(e)
        parents_count[e.downstream_id] += 1
        device_ids.update((e.upstream_id, e.downstream_id))

    devices = {
        d.pk: d for d in Device.objects.filter(pk__in=device_ids).select_related('site', 'role', 'device_type')
    }
    states = dict(DeviceStatus.objects.filter(device_id__in=device_ids).values_list('device_id', 'state'))

    def unique_descendants(device_id):
        seen, stack = set(), [device_id]
        while stack:
            for e in children_of.get(stack.pop(), ()):
                if e.downstream_id not in seen:
                    seen.add(e.downstream_id)
                    stack.append(e.downstream_id)
        return len(seen)

    def build(device_id, edge):
        kids = sorted(
            children_of.get(device_id, ()),
            key=lambda e: (devices[e.downstream_id].name or '').lower(),
        )
        return {
            'device': devices[device_id],
            'state': states.get(device_id),
            'edge': edge,
            'multi_parent': parents_count[device_id] > 1,
            'children': [build(e.downstream_id, e) for e in kids],
            'direct': len(kids),
        }

    root_ids = sorted(
        (i for i in device_ids if parents_count[i] == 0 and (not site_id or devices[i].site_id == site_id)),
        key=lambda i: (devices[i].name or '').lower(),
    )
    page = None
    selected = root_ids
    if per_page:
        page = EnhancedPaginator(root_ids, per_page).get_page(page_number)
        selected = list(page.object_list)
    roots = []
    for rid in selected:
        node = build(rid, None)
        node['total'] = unique_descendants(rid)
        roots.append(node)
    stats = {
        'devices': len(device_ids),
        'dependencies': len(edges),
        'roots': len(root_ids),
        'page': page,
        'multi_parent': sum(1 for c in parents_count.values() if c > 1),
    }
    return roots, stats
