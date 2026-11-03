"""The "clouds" view of the dependency graph: instead of devices, the graph shows one cloud per region (kraj, okres, obec, ...) or
per site, level by level. A cloud shows how many sites / devices are below it and how many are down; the links between the clouds
are the dependencies and peer links between their devices, merged into one line per pair of clouds. A click on a cloud opens the
level below it (a region -> its sub-regions and sites, a site -> its devices).

Which clouds are on a map: the sub-regions of the open region (or the top-level regions) that have devices in the graph, and the sites
that sit directly in it (the sites without a region are on the top level). Devices outside the open region are shown as "outside"
clouds, so the links that leave the region stay visible."""
from collections import Counter, defaultdict

from django.urls import reverse

from dcim.models import Device, Site

from .graph_data import _region_chain, _region_maps, graph_url
from .models import Dependency, PeerLink, TopologyDevice
from .status import status_snapshot, suppressed_info

MAX_ITEMS = 15            # links listed in the details of one line between two clouds
MAX_ROOTS = 12            # root causes listed above the graph


def build_overview(region_id=None):
    regions, _children = _region_maps()
    if region_id not in regions:
        region_id = None
    path = [r['id'] for r in _region_chain(region_id, regions)] if region_id else []

    dependencies = list(Dependency.objects.values_list('upstream_id', 'downstream_id', 'medium'))
    peers = list(PeerLink.objects.values_list('device_a_id', 'device_b_id', 'medium'))
    ids = set(TopologyDevice.objects.values_list('device_id', flat=True))
    for first, second, _medium in dependencies + peers:
        ids |= {first, second}
    devices = {d['id']: d for d in Device.objects.filter(pk__in=ids).values('id', 'name', 'site_id')}
    sites = {s['id']: s for s in Site.objects.filter(pk__in={d['site_id'] for d in devices.values()}).values('id', 'name', 'region_id')}
    states, roots = status_snapshot()
    suppressed = suppressed_info()

    chains = {}

    def chain_of(site_id):
        if site_id not in chains:
            rid = sites[site_id]['region_id']
            chains[site_id] = [r['id'] for r in _region_chain(rid, regions)] if rid else []
        return chains[site_id]

    def cloud_for(site_id):
        """(kind, id, outside) of the cloud that holds a site on the map of the open region."""
        chain = chain_of(site_id)
        if chain[:len(path)] == path:
            rest = chain[len(path):]
            return ('r', rest[0], False) if rest else ('s', site_id, False)
        common = 0
        while common < min(len(chain), len(path)) and chain[common] == path[common]:
            common += 1
        return ('r', chain[common], True) if common < len(chain) else ('s', site_id, True)

    clouds = {}

    def cloud(kind, pk, outside):
        key = f'{kind}{pk}'
        if key not in clouds:
            name = regions[pk]['name'] if kind == 'r' else sites[pk]['name']
            clouds[key] = {'key': key, 'kind': kind, 'pk': pk, 'name': name, 'outside': outside, 'sites': set(), 'devices': 0,
                           'down': 0, 'unknown': 0, 'suppressed': 0, 'up': 0}
        return clouds[key]

    cloud_of_device = {}
    for pk, device in devices.items():
        kind, target, outside = cloud_for(device['site_id'])
        c = cloud(kind, target, outside)
        cloud_of_device[pk] = c
        c['sites'].add(device['site_id'])
        c['devices'] += 1
        state = states.get(pk, ['none'])[0]
        if pk in suppressed:
            c['suppressed'] += 1
        elif state == 'down':
            c['down'] += 1
        elif state == 'up':
            c['up'] += 1
        else:
            c['unknown'] += 1

    pairs = {}

    def pair(first, second, peer):
        a, b = cloud_of_device[first], cloud_of_device[second]
        if a is b or (a['outside'] and b['outside']):
            return None
        if peer and a['key'] > b['key']:
            a, b = b, a
        key = (a['key'], b['key'], peer)
        if key not in pairs:
            pairs[key] = {'source': a, 'target': b, 'peer': peer, 'count': 0, 'media': Counter(), 'items': [], 'affected': False}
        return pairs[key]

    for first, second, medium in dependencies:
        p = pair(first, second, False)
        if p:
            p['count'] += 1
            p['media'][medium] += 1
            if len(p['items']) < MAX_ITEMS:
                p['items'].append({'from': devices[first]['name'], 'to': devices[second]['name'], 'medium': medium, 'peer': False})
            if states.get(first, ['none'])[0] == 'down' and first not in suppressed:
                p['affected'] = True
    for first, second, medium in peers:
        p = pair(first, second, True)
        if p:
            p['count'] += 1
            p['media'][medium] += 1
            if len(p['items']) < MAX_ITEMS:
                p['items'].append({'from': devices[first]['name'], 'to': devices[second]['name'], 'medium': medium, 'peer': True})

    linked = {key for pair_key in pairs for key in pair_key[:2]}
    nodes = []
    for c in sorted(clouds.values(), key=lambda c: (c['outside'], c['name'].lower())):
        if c['outside'] and c['key'] not in linked:
            continue                                   # an outside cloud is shown only when a link leads to it
        n_sites = len(c['sites'])
        if c['down']:
            state = 'down'
        elif c['unknown']:
            state = 'unknown'
        elif c['up']:
            state = 'up'
        else:
            state = 'none'
        info = (f"{n_sites} sites · " if c['kind'] == 'r' else '') + f"{c['devices']} dev" + (f" · {c['down']} down" if c['down'] else '')
        nodes.append({'data': {
            'id': c['key'], 'label': c['name'], 'info': info, 'state': state, 'cloud': True, 'kind': c['kind'], 'outside': c['outside'],
            'sites': n_sites, 'devices': c['devices'], 'down': c['down'], 'unknown': c['unknown'], 'suppressed': c['suppressed'],
            'map_url': graph_url(region=c['pk']) if c['kind'] == 'r' else graph_url(site=c['pk']),
        }})

    edges = []
    for (source, target, peer), p in sorted(pairs.items()):
        edges.append({'data': {
            'id': f"{'p' if peer else 'e'}:{source}>{target}", 'source': source, 'target': target, 'peer': peer, 'count': p['count'],
            'medium': p['media'].most_common(1)[0][0], 'media': dict(p['media']), 'items': p['items'], 'affected': p['affected'],
            'a_name': p['source']['name'], 'b_name': p['target']['name'],
            'elabel': f"{p['count']} links" if p['count'] > 1 else '',
        }})

    root_rows = []
    for root in sorted(roots, key=lambda r: -r['down_count'])[:MAX_ROOTS]:
        device = devices.get(root['id'])
        if device:
            root_rows.append({'name': root['name'], 'down': root['down_count'], 'site': sites[device['site_id']]['name'],
                              'cloud': cloud_of_device[root['id']]['key'],
                              'url': graph_url(site=device['site_id'], focus=root['id'], impact=1)})

    crumbs = [{'name': 'All', 'url': graph_url()}]
    title = 'All dependencies'
    if region_id:
        title = regions[region_id]['name']
        crumbs += [{'name': r['name'], 'url': graph_url(region=r['id'])} for r in _region_chain(region_id, regions)[:-1]]
    own = [n['data'] for n in nodes if not n['data']['outside']]
    return {
        'overview': True,
        'nodes': nodes, 'edges': edges, 'roots': root_rows, 'scope': f'region:{region_id}' if region_id else 'all',
        'title': title, 'crumbs': crumbs,
        'totals': {'clouds': len(own), 'devices': sum(n['devices'] for n in own), 'down': sum(n['down'] for n in own)},
        'devices_url': graph_url(region=region_id, view='devices'),
        'overview_api': reverse('plugins-api:netbox_dependencies-api:overview') + (f'?region={region_id}' if region_id else ''),
    }
