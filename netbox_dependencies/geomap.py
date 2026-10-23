"""Data for the geographic map: the sites that are in the topology, placed by their GPS coordinates, with their live state and the
links between sites. Everything is aggregated per site on the server, so the page stays small whatever the number of devices."""
from collections import Counter, defaultdict

from django.urls import reverse
from netbox.plugins import get_plugin_config

from dcim.models import Device, Site

from .graph_data import graph_url
from .models import Dependency, PeerLink, TopologyDevice
from .status import status_snapshot, suppressed_info

PLUGIN = 'netbox_dependencies'
MAX_ITEMS = 15            # links listed in the popup of one line between two sites
MAX_ROOTS = 5             # root causes listed in the popup of one site
BUILTIN_LAYERS = [
    {'name': 'Street', 'url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', 'attribution': '', 'max_zoom': 19, 'dark': True},
    {'name': 'Terrain', 'url': 'https://tile.opentopomap.org/{z}/{x}/{y}.png', 'max_zoom': 17, 'dark': False,
     'attribution': 'Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | '
                    'style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)'},
    {'name': 'Satellite', 'url': 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
     'max_zoom': 19, 'dark': False, 'attribution': 'Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community'},
]


def _layers():
    """The selectable map backgrounds: `map_layers` if set, otherwise the street layer (`map_tile_url`) and the built-in extras.
    `dark` = the layer is inverted in the dark theme (only sensible for light street maps)."""
    configured = get_plugin_config(PLUGIN, 'map_layers')
    if configured is not None:
        return [{'name': str(layer.get('name') or 'Map'), 'url': layer['url'], 'attribution': layer.get('attribution', ''),
                 'max_zoom': int(layer.get('max_zoom', 19)), 'dark': bool(layer.get('dark'))} for layer in configured if layer.get('url')]
    street = get_plugin_config(PLUGIN, 'map_tile_url')
    if not street:
        return []
    return [dict(BUILTIN_LAYERS[0], url=street, attribution=get_plugin_config(PLUGIN, 'map_attribution') or '',
                 max_zoom=int(get_plugin_config(PLUGIN, 'map_max_zoom')))] + BUILTIN_LAYERS[1:]


def _settings():
    layers = _layers()
    return {
        'layers': layers,
        'tile_url': layers[0]['url'] if layers else '',          # kept for older clients
        'attribution': layers[0]['attribution'] if layers else '',
        'max_zoom': int(get_plugin_config(PLUGIN, 'map_max_zoom')),
        'center': list(get_plugin_config(PLUGIN, 'map_default_center')),
        'zoom': int(get_plugin_config(PLUGIN, 'map_default_zoom')),
    }


def build_map():
    """Returns {'sites': [...], 'links': [...], 'missing': [...], 'settings': {...}} (JSON-serialisable)."""
    dependencies = list(Dependency.objects.values_list('upstream_id', 'downstream_id', 'medium'))
    peers = list(PeerLink.objects.values_list('device_a_id', 'device_b_id', 'medium'))
    ids = set(TopologyDevice.objects.values_list('device_id', flat=True))
    for first, second, _medium in dependencies + peers:
        ids |= {first, second}
    devices = {d['id']: d for d in Device.objects.filter(pk__in=ids).values('id', 'name', 'site_id')}

    states, roots = status_snapshot()
    suppressed = suppressed_info()
    per_site = defaultdict(lambda: Counter())
    for pk, device in devices.items():
        counts = per_site[device['site_id']]
        counts['devices'] += 1
        state = states.get(pk, ['none'])[0]
        if pk in suppressed:
            counts['suppressed'] += 1
        elif state == 'down':
            counts['down'] += 1
        elif state in ('unknown', 'none'):
            counts['unknown'] += 1
    site_roots = defaultdict(list)
    for root in roots:
        device = devices.get(root['id'])
        if device:
            site_roots[device['site_id']].append({'id': root['id'], 'name': root['name'], 'down': root['down_count']})

    site_rows = {s['id']: s for s in Site.objects.filter(pk__in=per_site.keys()).values(
        'id', 'name', 'latitude', 'longitude', 'physical_address', 'region__name')}
    sites, missing = [], []
    for site_id, counts in per_site.items():
        row = site_rows.get(site_id)
        if not row:
            continue
        entry = {
            'id': site_id, 'name': row['name'], 'region': row['region__name'] or '', 'address': (row['physical_address'] or '').strip(),
            'devices': counts['devices'], 'down': counts['down'], 'unknown': counts['unknown'], 'suppressed': counts['suppressed'],
            'state': 'down' if counts['down'] else 'unknown' if counts['unknown'] else 'up',
            'roots': sorted(site_roots.get(site_id, ()), key=lambda r: -r['down'])[:MAX_ROOTS], 'root_count': len(site_roots.get(site_id, ())),
            'graph_url': graph_url(site=site_id), 'url': reverse('dcim:site', args=[site_id]),
        }
        if row['latitude'] is None or row['longitude'] is None:
            entry['edit_url'] = reverse('dcim:site_edit', args=[site_id])
            missing.append(entry)
        else:
            entry['lat'], entry['lon'] = float(row['latitude']), float(row['longitude'])
            sites.append(entry)

    # one line per pair of sites that are linked by at least one dependency / peer link
    placed = {s['id'] for s in sites}
    pairs = defaultdict(lambda: {'count': 0, 'media': Counter(), 'items': [], 'affected': False, 'peers': 0})
    hidden = 0
    for first, second, medium, is_peer in [(a, b, m, False) for a, b, m in dependencies] + [(a, b, m, True) for a, b, m in peers]:
        site_a, site_b = devices[first]['site_id'], devices[second]['site_id']
        if site_a == site_b:
            continue
        if site_a not in placed or site_b not in placed:
            hidden += 1
            continue
        pair = pairs[tuple(sorted((site_a, site_b)))]
        pair['count'] += 1
        pair['media'][medium] += 1
        pair['peers'] += 1 if is_peer else 0
        if len(pair['items']) < MAX_ITEMS:
            pair['items'].append({'from': devices[first]['name'], 'to': devices[second]['name'], 'medium': medium, 'peer': is_peer})
        if not is_peer and states.get(first, ['none'])[0] == 'down' and first not in suppressed:
            pair['affected'] = True           # the upstream end of a dependency is down
    names = {s['id']: s['name'] for s in sites}
    links = [{
        'a': a, 'b': b, 'a_name': names[a], 'b_name': names[b], 'count': pair['count'], 'peers': pair['peers'],
        'medium': pair['media'].most_common(1)[0][0], 'media': dict(pair['media']), 'items': pair['items'], 'affected': pair['affected'],
    } for (a, b), pair in sorted(pairs.items())]

    sites.sort(key=lambda s: s['name'].lower())
    missing.sort(key=lambda s: s['name'].lower())
    return {'sites': sites, 'links': links, 'links_hidden': hidden, 'missing': missing, 'settings': _settings()}
