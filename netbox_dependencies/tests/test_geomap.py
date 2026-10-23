"""Geographic map: sites of the topology on a free map by their GPS coordinates."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from dcim.models import Device, Site
from netbox_dependencies.geomap import build_map
from netbox_dependencies.models import DeviceStatus, DeviceSuppression, PeerLink
from netbox_dependencies.tests.base import Base

API = '/api/plugins/dependencies'


def set_state(device, state):
    now = timezone.now()
    DeviceStatus.objects.update_or_create(device=device, defaults=dict(
        state=state, source='icmp', detail='', last_checked=now, last_changed=now - timedelta(minutes=10)))


class GeoMapTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('gm', 'gm@example.com', 'x'))
        base = self.devs['core']
        self.site_a = base.site
        self.site_a.latitude, self.site_a.longitude = Decimal('48.7164'), Decimal('18.5826')
        self.site_a.physical_address = 'Main street 1\nTown'
        self.site_a.save()
        self.site_b = Site.objects.create(name='Remote', slug='remote', latitude=Decimal('48.1486'), longitude=Decimal('17.1077'))
        self.site_c = Site.objects.create(name='No GPS', slug='nogps')
        for name, site in (('sw1', self.site_b), ('sw2', self.site_c)):
            self.devs[name].site = site
            self.devs[name].save()
        self.dep('core', 'sw1')          # site A -> site B
        self.dep('core', 'sw2')          # site A -> a site without coordinates
        self.dep('sw1', 'ap')            # site B -> site A (ap stays on site A)

    def sites(self, data):
        return {s['name']: s for s in data['sites']}

    def test_sites_are_placed_by_their_coordinates_and_the_missing_ones_listed(self):
        data = build_map()
        sites = self.sites(data)
        self.assertEqual(set(sites), {self.site_a.name, 'Remote'})
        a = sites[self.site_a.name]
        self.assertEqual((a['lat'], a['lon'], a['devices']), (48.7164, 18.5826, 2))      # core and ap
        self.assertTrue(a['graph_url'].endswith(f'?site={self.site_a.pk}'))
        self.assertEqual([m['name'] for m in data['missing']], ['No GPS'])
        self.assertTrue(data['missing'][0]['edit_url'].endswith(f'/sites/{self.site_c.pk}/edit/'))
        self.assertEqual(data['settings']['tile_url'], 'https://tile.openstreetmap.org/{z}/{x}/{y}.png')

    def test_live_state_per_site_and_root_causes(self):
        set_state(self.devs['sw1'], 'down')
        set_state(self.devs['core'], 'up')
        set_state(self.devs['ap'], 'unknown')
        data = build_map()
        sites = self.sites(data)
        remote = sites['Remote']
        self.assertEqual((remote['state'], remote['down'], remote['root_count']), ('down', 1, 1))
        self.assertEqual(remote['roots'][0]['name'], 'sw1')
        self.assertEqual(sites[self.site_a.name]['state'], 'unknown')

    def test_a_suppressed_down_device_does_not_make_its_site_red(self):
        set_state(self.devs['sw1'], 'down')
        DeviceSuppression.objects.create(device=self.devs['sw1'], reason='backup')
        remote = self.sites(build_map())['Remote']
        self.assertEqual((remote['down'], remote['suppressed'], remote['root_count']), (0, 1, 0))
        self.assertNotEqual(remote['state'], 'down')

    def test_links_between_sites_are_aggregated_and_flagged(self):
        data = build_map()
        self.assertEqual(len(data['links']), 1)                     # one line A <-> B (the link to the site without GPS is hidden)
        line = data['links'][0]
        self.assertEqual((line['count'], line['affected']), (2, False))
        self.assertEqual(sorted(line['a_name'] + line['b_name']), sorted('Remote' + self.site_a.name))
        self.assertEqual(sorted((i['from'], i['to']) for i in line['items']), [('core', 'sw1'), ('sw1', 'ap')])
        self.assertEqual(data['links_hidden'], 1)
        set_state(self.devs['core'], 'down')                        # the upstream end of core -> sw1 is down
        self.assertTrue(build_map()['links'][0]['affected'])

    def test_peer_links_and_same_site_links(self):
        self.dep('sw2', 'sw1')                                      # no GPS on one end: hidden
        PeerLink.objects.create(device_a=self.devs['core'], device_b=self.devs['ap'])          # same site: not a line
        extra = Device.objects.create(name='x2', site=self.site_b, device_type=self.devs['core'].device_type, role=self.devs['core'].role)
        PeerLink.objects.create(device_a=self.devs['ap'], device_b=extra)
        line = build_map()['links'][0]
        self.assertEqual(line['count'], 3)
        self.assertEqual(line['peers'], 1)
        self.assertTrue(any(i['peer'] for i in line['items']))

    def test_api_page_assets_and_permission(self):
        body = self.client.get(f'{API}/map/').json()
        self.assertEqual({s['name'] for s in body['sites']}, {self.site_a.name, 'Remote'})
        page = self.client.get('/plugins/dependencies/map/').content.decode()
        self.assertIn('id="gm-map"', page)
        self.assertIn('/plugins/dependencies/map/leaflet.js', page)
        self.assertIn('"map_api": "/api/plugins/dependencies/map/"', page)
        js = self.client.get('/plugins/dependencies/map/leaflet.js')
        css = self.client.get('/plugins/dependencies/map/leaflet.css')
        self.assertEqual((js.status_code, css.status_code), (200, 200))
        self.assertIn('javascript', js['Content-Type'])
        self.assertIn('text/css', css['Content-Type'])
        nobody = Client()
        nobody.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        self.assertEqual(nobody.get('/plugins/dependencies/map/').status_code, 403)
        self.assertEqual(nobody.get(f'{API}/map/').status_code, 403)

    def test_the_graph_has_a_button_to_the_map(self):
        self.assertIn('/plugins/dependencies/map/', self.client.get('/plugins/dependencies/graph/').content.decode())
