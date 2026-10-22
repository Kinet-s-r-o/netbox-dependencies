"""Devices put into the topology from the graph, and the API behind the graph's edit mode."""
import json

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import Client

from dcim.models import Region, Site
from ipam.models import IPAddress
from netbox_dependencies.graph_data import build_graph, build_sidebar
from netbox_dependencies.models import CustomMap, Dependency, TopologyDevice
from netbox_dependencies.status import poll_targets
from netbox_dependencies.tests.base import Base
from netbox_dependencies.tree import build_tree
from users.models import ObjectPermission

API = '/api/plugins/dependencies'


class TopologyTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('tp', 'tp@example.com', 'x'))
        self.dep('core', 'sw1')

    def post(self, body, url=f'{API}/topology/'):
        return self.client.post(url, data=json.dumps(body), content_type='application/json')

    def labels(self, **kwargs):
        return {n['data']['label'] for n in build_graph(**kwargs)['nodes']}

    def test_adding_a_device_puts_it_on_the_maps_of_its_location(self):
        r = self.post({'device': self.devs['sw2'].pk, 'scope': 'all'})
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual((body['created'], body['in_scope'], body['node']['label'], body['node']['topo']), (True, True, 'sw2', True))
        self.assertEqual((body['node']['ups'], body['node']['downs'], body['node']['site_id']), (0, 0, self.devs['sw2'].site_id))
        again = self.post({'device': self.devs['sw2'].pk, 'scope': 'all'})
        self.assertEqual((again.status_code, again.json()['created']), (200, False))             # idempotent
        self.assertEqual(self.labels(), {'core', 'sw1', 'sw2'})                                   # on "All" without any dependency
        self.assertIn('sw2', self.labels(site_id=self.devs['sw2'].site_id))
        self.assertEqual(sum(1 for n in build_graph()['nodes'] if n['data'].get('topo')), 1)      # only the added one is marked

    def test_other_locations_and_regions_and_custom_maps(self):
        region = Region.objects.create(name='R', slug='r')
        other = Site.objects.create(name='Other', slug='other', region=region)
        self.devs['sw2'].site = other
        self.devs['sw2'].save()
        self.post({'device': self.devs['sw2'].pk, 'scope': 'all'})
        self.assertNotIn('sw2', self.labels(site_id=self.devs['core'].site_id))                   # not a cloud either
        self.assertIn('sw2', self.labels(region_id=region.pk))
        r = self.post({'device': self.devs['sw2'].pk, 'scope': f'site:{self.devs["core"].site_id}'})
        self.assertFalse(r.json()['in_scope'])                                                    # it appears on the map of its own location
        custom = CustomMap.objects.create(name='mine')
        r = self.post({'device': self.devs['ap'].pk, 'scope': f'map:{custom.pk}'})
        self.assertEqual((r.json()['in_scope'], r.json()['added_to_map']), (True, True))
        self.assertIn('ap', self.labels(map_id=custom.pk))

    def test_sidebar_tree_and_removal(self):
        self.post({'device': self.devs['sw2'].pk, 'scope': 'all'})
        self.assertEqual(build_sidebar()['total'], 3)
        roots, _stats = build_tree()
        self.assertEqual(sorted(r['device'].name for r in roots), ['core', 'sw2'])
        r = self.client.delete(f'{API}/topology/{self.devs["sw2"].pk}/')
        self.assertEqual((r.status_code, r.json()), (200, {'removed': True, 'still_linked': False}))
        self.assertNotIn('sw2', self.labels())
        # a device with dependencies stays on the maps because of them
        self.post({'device': self.devs['core'].pk, 'scope': 'all'})
        self.assertTrue(self.client.delete(f'{API}/topology/{self.devs["core"].pk}/').json()['still_linked'])
        self.assertIn('core', self.labels())

    def test_poller_targets_include_added_devices(self):
        ip = IPAddress.objects.create(address='10.1.1.1/24')
        self.devs['sw2'].primary_ip4 = ip
        self.devs['sw2'].save()
        self.assertEqual([d.name for d, _ip in poll_targets()], [])             # no dependency, not in the topology
        self.post({'device': self.devs['sw2'].pk, 'scope': 'all'})
        self.assertEqual([d.name for d, _ip in poll_targets()], ['sw2'])

    def test_search(self):
        TopologyDevice.objects.create(device=self.devs['ap'])
        rows = self.client.get(f'{API}/topology/search/?q=sw').json()['results']
        self.assertEqual(sorted((r['name'], r['on_map']) for r in rows), [('sw1', True), ('sw2', False)])
        self.assertEqual(self.client.get(f'{API}/topology/search/?q=a').json()['results'], [])       # too short
        found = self.client.get(f'{API}/topology/search/?q=ap').json()['results']
        self.assertTrue(next(r for r in found if r['name'] == 'ap')['on_map'])

    def test_permissions_and_bad_input(self):
        viewer = Client()
        user = get_user_model().objects.create_user('ro', 'ro@example.com', 'x')
        permission = ObjectPermission.objects.create(name='read', actions=['view'])
        permission.object_types.set([ContentType.objects.get_for_model(Dependency), ContentType.objects.get_for_model(self.devs['core'])])
        permission.users.add(user)
        viewer.force_login(user)
        r = viewer.post(f'{API}/topology/', data=json.dumps({'device': self.devs['sw2'].pk}), content_type='application/json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(viewer.delete(f'{API}/topology/{self.devs["sw2"].pk}/').status_code, 403)
        self.assertEqual(self.post({'device': 'abc'}).status_code, 400)
        self.assertEqual(self.post({'device': 999999}).status_code, 400)

    def test_graph_page_tells_the_page_what_the_user_may_do(self):
        page = self.client.get('/plugins/dependencies/graph/').content.decode()
        for needle in ('"can_edit": true', '"can_delete": true', '"topology_url":', '"search_url":'):
            self.assertIn(needle, page)
        # the edit bar floats inside the canvas (it must not push the graph down) together with the undo messages
        self.assertLess(page.index('id="dep-graph"'), page.index('id="dep-editbar"'))
        self.assertIn('id="dep-edit-toasts"', page)
        self.assertIn('"live_url": "/plugins/dependencies/live/"', page)       # the compact outages banner links to the full list
