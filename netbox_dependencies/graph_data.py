"""Data for the visual graph (Cytoscape.js): maps per location / region.

Map (scope):
  * 'all'        — all dependencies,
  * 'site:<id>'  — devices of the given location,
  * 'region:<id>' — devices of all locations in the region and its sub-regions.
Devices outside the map that are linked to it are shown as "clouds" (one per location)
(external) — a click opens the map of their location.
"""
from collections import Counter, defaultdict

from django.urls import reverse

from dcim.models import Device, Region, Site

from .icons import graph_icons
from .layout import compose
from .models import CustomMap, Dependency, DeviceStatus, NodePosition
from .status import down_device_ids, suppressed_info


def scope_key(site_id=None, region_id=None):
    if site_id:
        return f'site:{site_id}'
    if region_id:
        return f'region:{region_id}'
    return 'all'


def _closure(start, adjacency):
    seen, stack = set(), [start]
    while stack:
        for nxt in adjacency.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _region_maps():
    regions = {r['id']: r for r in Region.objects.values('id', 'name', 'parent_id')}
    children = defaultdict(list)
    for r in regions.values():
        children[r['parent_id']].append(r['id'])
    return regions, children


def _region_subtree(region_id, children):
    ids, stack = set(), [region_id]
    while stack:
        rid = stack.pop()
        if rid in ids:
            continue
        ids.add(rid)
        stack.extend(children.get(rid, ()))
    return ids


def _region_chain(region_id, regions):
    chain = []
    while region_id:
        r = regions[region_id]
        chain.append(r)
        region_id = r['parent_id']
    return list(reversed(chain))


def _layout_from_children(key, node_ids, regions, region_children):
    """The layout of a map without a layout of its own, taken over from its sub-maps: a region from its sub-regions and the
    sites directly in it, the "All" map from the top-level regions and the sites without a region. A sub-region that has no
    layout either is composed from its own sub-maps. Returns {} when nothing below has a layout."""
    saved = defaultdict(dict)
    for scope, node, x, y in NodePosition.objects.filter(node__startswith='d').values_list('scope', 'node', 'x', 'y'):
        saved[scope][node] = (x, y)
    sites = list(Site.objects.values('id', 'name', 'region_id'))
    wanted = set(node_ids)

    def children(scope):
        if scope == 'all':
            regs = [r for r in regions.values() if r['parent_id'] is None]
            leaf = [s for s in sites if s['region_id'] is None]
        else:
            rid = int(scope.split(':')[1])
            regs = [regions[c] for c in region_children.get(rid, ())]
            leaf = [s for s in sites if s['region_id'] == rid]
        named = [(r['name'].lower(), f"region:{r['id']}") for r in regs] + [(s['name'].lower(), f"site:{s['id']}") for s in leaf]
        return [scope_ for _name, scope_ in sorted(named)]

    def layout_of(scope):
        own = {n: p for n, p in saved.get(scope, {}).items() if n in wanted}
        if own or scope.startswith('site:'):
            return own
        return compose([layout_of(c) for c in children(scope)], node_ids, leftovers=False)

    return compose([layout_of(c) for c in children(key)], node_ids)


def graph_url(**params):
    base = reverse('plugins:netbox_dependencies:graph')
    query = '&'.join(f'{k}={v}' for k, v in params.items() if v)
    return f'{base}?{query}' if query else base


def build_graph(site_id=None, region_id=None, device_id=None, focus_id=None, map_id=None):
    """Returns a dict with nodes, edges, navigation items and saved positions."""
    custom = CustomMap.objects.filter(pk=map_id).first() if map_id else None
    key = f'map:{custom.pk}' if custom else scope_key(site_id, region_id)
    all_edges = list(Dependency.objects.select_related('upstream', 'downstream'))
    regions, region_children = _region_maps()

    edges = all_edges
    if device_id:  # only the branch of the device: ancestors + descendants
        up, down = defaultdict(set), defaultdict(set)
        for e in all_edges:
            down[e.upstream_id].add(e.downstream_id)
            up[e.downstream_id].add(e.upstream_id)
        keep = {device_id} | _closure(device_id, up) | _closure(device_id, down)
        edges = [e for e in all_edges if e.upstream_id in keep and e.downstream_id in keep]

    ids = {e.upstream_id for e in edges} | {e.downstream_id for e in edges}
    members = set(custom.devices.values_list('pk', flat=True)) if custom else None
    if members:
        ids |= members          # members of a custom map are shown even without dependencies
    devices = {
        d.pk: d for d in Device.objects.filter(pk__in=ids).select_related(
            'site__region', 'role', 'device_type', 'primary_ip4', 'primary_ip6', 'location', 'tenant')
    }

    in_scope = set(ids)
    if site_id:
        in_scope = {pk for pk, d in devices.items() if d.site_id == site_id}
    elif region_id:
        rids = _region_subtree(region_id, region_children)
        in_scope = {pk for pk, d in devices.items() if d.site.region_id in rids}
    elif members is not None:
        in_scope = members

    edges = [e for e in edges if e.upstream_id in in_scope or e.downstream_id in in_scope]
    ids = {e.upstream_id for e in edges} | {e.downstream_id for e in edges}
    if members:
        ids |= members
    external = ids - in_scope
    status_rows = {
        r[0]: r[1:] for r in DeviceStatus.objects.filter(device_id__in=ids).values_list(
            'device_id', 'state', 'source', 'detail', 'last_changed')
    }
    states = {pk: row[0] for pk, row in status_rows.items()}
    upstream_count, downstream_count = Counter(), Counter()   # counts in the whole graph, not only on the map
    for e in all_edges:
        downstream_count[e.upstream_id] += 1
        upstream_count[e.downstream_id] += 1

    suppressed = suppressed_info()
    nodes = []
    for pk in sorted(ids - external, key=lambda i: (devices[i].name or '').lower()):
        d = devices[pk]
        nodes.append({'data': {
            'id': f'd{pk}',
            'suppressed': suppressed.get(pk),
            'devid': pk,
            'label': d.name or f'#{pk}',
            'site': d.site.name,
            'role': d.role.name if d.role else '',
            'dtype': d.device_type.model if d.device_type_id else '',
            'ip': str((d.primary_ip4 or d.primary_ip6).address.ip) if (d.primary_ip4 or d.primary_ip6) else '',
            'location': d.location.name if d.location_id else '',
            'tenant': d.tenant.name if d.tenant_id else '',
            'serial': d.serial or '',
            'description': d.description or '',
            'comments': (d.comments or '')[:1000],
            'comments_more': len(d.comments or '') > 1000,
            'ups': upstream_count[pk],
            'downs': downstream_count[pk],
            'state': states.get(pk, 'none'),
            'source': status_rows[pk][1] if pk in status_rows else '',
            'detail': status_rows[pk][2] if pk in status_rows else '',
            'since': status_rows[pk][3].isoformat() if pk in status_rows else '',
            'external': False,
            'cloud': False,
            'focus': pk in (device_id, focus_id),
            'url': d.get_absolute_url(),
        }})

    # devices from other locations are merged into one cloud per location
    clouds = defaultdict(list)
    for pk in external:
        clouds[devices[pk].site_id].append(pk)
    remote_map = {}  # id of a remote device -> id of its cloud (for the outage simulation)
    for site_pk, members in sorted(clouds.items(), key=lambda kv: devices[kv[1][0]].site.name.lower()):
        site = devices[members[0]].site
        member_states = [states.get(m, 'none') for m in members]
        down_count = member_states.count('down')
        cloud_state = (
            'down' if down_count
            else 'up' if all(s == 'up' for s in member_states)
            else 'unknown' if any(s == 'unknown' for s in member_states)
            else 'none'
        )
        for m in members:
            remote_map[m] = f'x{site_pk}'
        nodes.append({'data': {
            'id': f'x{site_pk}',
            'label': site.name,
            'info': f'{len(members)} linked' + (f' · {down_count} down' if down_count else ''),
            'site': site.name,
            'state': cloud_state,
            'external': True,
            'cloud': True,
            'focus': False,
            'map_url': graph_url(site=site_pk, focus=members[0] if len(members) == 1 else None),
        }})

    def endpoint(device_pk):
        return f'x{devices[device_pk].site_id}' if device_pk in external else f'd{device_pk}'

    graph_edges = []
    for e in edges:
        remote = None
        if e.upstream_id in external:
            remote = devices[e.upstream_id].name
        elif e.downstream_id in external:
            remote = devices[e.downstream_id].name
        label = e.redundancy_group
        if remote:
            label = f'{remote} ⇄ {label}' if label else remote
        graph_edges.append({'data': {
            'id': f'e{e.pk}',
            'source': endpoint(e.upstream_id),
            'target': endpoint(e.downstream_id),
            'pk': e.pk,
            'type': e.dependency_type,
            'medium': e.medium,
            'group': e.redundancy_group,
            'description': e.description,
            'elabel': label,
            'url': e.get_absolute_url(),
            'edit_url': f'{e.get_absolute_url()}edit/',
        }})

    # navigation: parent locations + neighbouring maps
    crumbs = [{'name': 'All', 'url': graph_url()}]
    title = 'All dependencies'
    if site_id:
        site = Site.objects.select_related('region').filter(pk=site_id).first()
        if site:
            title = site.name
            if site.region_id:
                crumbs += [{'name': r['name'], 'url': graph_url(region=r['id'])}
                           for r in _region_chain(site.region_id, regions)]
    elif custom:
        title = custom.name
        crumbs.append({'name': 'Custom maps', 'url': reverse('plugins:netbox_dependencies:custommap_list')})
    elif region_id and region_id in regions:
        chain = _region_chain(region_id, regions)
        title = regions[region_id]['name']
        crumbs += [{'name': r['name'], 'url': graph_url(region=r['id'])} for r in chain[:-1]]

    neighbours = Counter()
    for e in edges:
        for pk in (e.upstream_id, e.downstream_id):
            if pk in external:
                neighbours[devices[pk].site_id] += 1
    neighbour_sites = {s.pk: s for s in Site.objects.filter(pk__in=neighbours.keys())}
    neighbour_list = sorted(
        ({'name': neighbour_sites[sid].name, 'links': n, 'url': graph_url(site=sid)}
         for sid, n in neighbours.items()),
        key=lambda x: (-x['links'], x['name']),
    )

    positions = {p.node: [p.x, p.y] for p in NodePosition.objects.filter(scope=key)}
    inherited, inherited_from = False, ''
    if not positions and not custom and (key == 'all' or key.startswith('region:')):
        # a parent map without a layout of its own takes over the layouts of its sub-maps, side by side
        positions = _layout_from_children(key, [n['data']['id'] for n in nodes], regions, region_children)
        inherited, inherited_from = bool(positions), 'children'
    if not positions and key != 'all':
        # the map has no layout of its own -> start from the layout of the "All" map
        positions = {p.node: [p.x, p.y] for p in NodePosition.objects.filter(scope='all', node__startswith='d')}
        inherited, inherited_from = bool(positions), 'all'
    return {
        'nodes': nodes,
        'edges': graph_edges,
        'scope': key,
        'title': title,
        'crumbs': crumbs,
        'neighbours': neighbour_list,
        'positions': positions,
        'inherited': inherited,
        'inherited_from': inherited_from,
        'custom_map': {
            'id': custom.pk, 'name': custom.name,
            'edit_url': reverse('plugins:netbox_dependencies:custommap_edit', args=[custom.pk]),
        } if custom else None,
        'remote_map': remote_map,
        'impact_url': reverse('plugins-api:netbox_dependencies-api:impact'),
        'states_url': reverse('plugins-api:netbox_dependencies-api:status_states'),
        'dependency_api': reverse('plugins-api:netbox_dependencies-api:dependency-list'),
        'icons': graph_icons(),
        'layout_url': reverse('plugins:netbox_dependencies:graph_layout'),
    }


def build_sidebar(site_id=None, region_id=None, map_id=None):
    """Tree Region -> Region -> Location with the number of devices in the graph and the number of outages.
    Empty branches are omitted."""
    edges = Dependency.objects.values_list('upstream_id', 'downstream_id')
    device_ids = {i for pair in edges for i in pair}
    down = down_device_ids() & device_ids
    per_site = Counter()
    per_site_down = Counter()
    for pk, site_pk in Device.objects.filter(pk__in=device_ids).values_list('pk', 'site_id'):
        per_site[site_pk] += 1
        if pk in down:
            per_site_down[site_pk] += 1

    regions, region_children = _region_maps()
    sites_by_region = defaultdict(list)
    for s in Site.objects.filter(pk__in=per_site.keys()).values('id', 'name', 'region_id'):
        sites_by_region[s['region_id']].append(s)

    def site_node(s):
        return {
            'kind': 'site', 'id': s['id'], 'name': s['name'],
            'count': per_site[s['id']], 'down': per_site_down[s['id']],
            'url': graph_url(site=s['id']), 'active': s['id'] == site_id, 'open': s['id'] == site_id,
            'children': [],
        }

    def region_node(rid):
        kids = [region_node(c) for c in sorted(region_children.get(rid, ()), key=lambda c: regions[c]['name'])]
        kids = [k for k in kids if k]
        sites = [site_node(s) for s in sorted(sites_by_region.get(rid, ()), key=lambda s: s['name'])]
        children = kids + sites
        if not children:
            return None
        return {
            'kind': 'region', 'id': rid, 'name': regions[rid]['name'],
            'count': sum(c['count'] for c in children), 'down': sum(c['down'] for c in children),
            'url': graph_url(region=rid), 'active': rid == region_id,
            'open': rid == region_id or any(c['open'] for c in children), 'children': children,
        }

    top = [region_node(r) for r in sorted(region_children.get(None, ()), key=lambda r: regions[r]['name'])]
    tree = [n for n in top if n]
    unassigned = [site_node(s) for s in sorted(sites_by_region.get(None, ()), key=lambda s: s['name'])]
    if unassigned:
        tree.append({
            'kind': 'group', 'id': 0, 'name': 'No region',
            'count': sum(c['count'] for c in unassigned), 'down': sum(c['down'] for c in unassigned),
            'url': None, 'active': False, 'open': any(c['open'] for c in unassigned), 'children': unassigned,
        })
    all_down = down_device_ids()
    members = defaultdict(set)
    for map_pk, device_pk in CustomMap.devices.through.objects.values_list('custommap_id', 'device_id'):
        members[map_pk].add(device_pk)
    custom = [
        {'id': m.pk, 'name': m.name, 'count': len(members[m.pk]), 'down': len(members[m.pk] & all_down),
         'url': graph_url(map=m.pk), 'active': m.pk == map_id}
        for m in CustomMap.objects.order_by('name')
    ]
    return {
        'tree': tree,
        'custom': custom,
        'total': len(device_ids),
        'total_down': len(down),
    }
