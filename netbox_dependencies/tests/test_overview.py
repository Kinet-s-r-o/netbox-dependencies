"""The clouds view of the dependency graph: regions and sites level by level."""
from django.contrib.auth import get_user_model
from django.test import Client

from dcim.models import Region, Site
from netbox_dependencies.overview import build_overview
from netbox_dependencies.models import DeviceSuppression, PeerLink
from netbox_dependencies.tests.base import Base
from netbox_dependencies.tests.test_geomap import set_state

API = '/api/plugins/dependencies'


class OverviewTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('ov', 'ov@example.com', 'x'))
        self.kraj = Region.objects.create(name='Kraj', slug='kraj')
        self.okres1 = Region.objects.create(name='Okres 1', slug='okres-1', parent=self.kraj)
        self.okres2 = Region.objects.create(name='Okres 2', slug='okres-2', parent=self.kraj)
        self.s1 = Site.objects.create(name='Site 1', slug='site-1', region=self.okres1)
        self.s2 = Site.objects.create(name='Site 2', slug='site-2', region=self.okres1)
        self.s3 = Site.objects.create(name='Site 3', slug='site-3', region=self.okres2)
        self.s4 = Site.objects.create(name='Site 4', slug='site-4')                    # no region
        for name, site in (('core', self.s1), ('sw1', self.s2), ('sw2', self.s3), ('ap', self.s4)):
            self.devs[name].site = site
            self.devs[name].save()
        self.dep('core', 'sw1')          # between two sites of the same okres
        self.dep('core', 'sw2')          # to another okres of the same kraj
        self.dep('sw1', 'ap')            # to a site without a region

    def nodes(self, data):
        return {n['data']['label']: n['data'] for n in data['nodes']}

    def test_top_level_has_the_regions_and_the_sites_without_one(self):
        data = build_overview()
        nodes = self.nodes(data)
        self.assertEqual(set(nodes), {'Kraj', 'Site 4'})
        kraj = nodes['Kraj']
        self.assertEqual((kraj['kind'], kraj['sites'], kraj['devices'], kraj['outside']), ('r', 3, 3, False))
        self.assertTrue(kraj['map_url'].endswith(f'?region={self.kraj.pk}'))
        self.assertTrue(nodes['Site 4']['map_url'].endswith(f'?site={self.s4.pk}'))
        self.assertEqual(data['totals'], {'clouds': 2, 'devices': 4, 'down': 0})
        self.assertEqual(len(data['edges']), 1)                                 # the links inside Kraj are not drawn
        edge = data['edges'][0]['data']
        self.assertEqual((edge['a_name'], edge['b_name'], edge['count']), ('Kraj', 'Site 4', 1))

    def test_inside_a_region_the_next_level_and_the_outside_neighbours(self):
        data = build_overview(self.okres1.pk)
        nodes = self.nodes(data)
        self.assertEqual(set(nodes), {'Site 1', 'Site 2', 'Okres 2', 'Site 4'})
        self.assertEqual([n for n, d in nodes.items() if d['outside']], ['Okres 2', 'Site 4'])
        self.assertEqual([c['name'] for c in data['crumbs']], ['All', 'Kraj'])
        self.assertEqual(data['title'], 'Okres 1')
        links = {(e['data']['a_name'], e['data']['b_name']): e['data']['count'] for e in data['edges']}
        self.assertEqual(links, {('Site 1', 'Site 2'): 1, ('Site 1', 'Okres 2'): 1, ('Site 2', 'Site 4'): 1})
        top = build_overview(self.kraj.pk)
        self.assertEqual(set(self.nodes(top)), {'Okres 1', 'Okres 2', 'Site 4'})
        self.assertEqual(sorted(self.nodes(top)['Okres 1'][k] for k in ('sites', 'devices')), [2, 2])

    def test_state_of_a_cloud_and_aggregated_links(self):
        set_state(self.devs['core'], 'down')
        data = build_overview(self.okres1.pk)
        nodes = self.nodes(data)
        self.assertEqual((nodes['Site 1']['state'], nodes['Site 1']['down']), ('down', 1))
        self.assertEqual(nodes['Site 2']['state'], 'unknown')                     # no status yet
        affected = {(e['data']['a_name'], e['data']['b_name']): e['data']['affected'] for e in data['edges']}
        self.assertTrue(affected[('Site 1', 'Site 2')])
        self.assertFalse(affected[('Site 2', 'Site 4')])
        self.assertEqual([r['name'] for r in data['roots']], ['core'])
        self.assertIn('focus=', data['roots'][0]['url'])
        DeviceSuppression.objects.create(device=self.devs['core'], reason='backup')
        data = build_overview(self.okres1.pk)
        self.assertEqual((self.nodes(data)['Site 1']['down'], self.nodes(data)['Site 1']['suppressed']), (0, 1))

    def test_peer_links_are_drawn_without_a_direction(self):
        PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'])
        peers = [e['data'] for e in build_overview(self.okres1.pk)['edges'] if e['data']['peer']]
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]['count'], 1)
        self.assertEqual(sorted((peers[0]['a_name'], peers[0]['b_name'])), ['Okres 2', 'Site 2'])

    def test_unknown_region_means_the_top_level(self):
        self.assertEqual(set(self.nodes(build_overview(99999))), {'Kraj', 'Site 4'})

    def test_page_and_api_default_to_the_clouds(self):
        page = self.client.get('/plugins/dependencies/graph/').content.decode()
        self.assertIn('"overview": true', page)
        self.assertIn('depOverview', page)
        self.assertIn('?view=devices', page)                                      # the switch to the devices
        body = self.client.get(f'{API}/overview/?region={self.okres1.pk}').json()
        self.assertEqual({n['data']['label'] for n in body['nodes']}, {'Site 1', 'Site 2', 'Okres 2', 'Site 4'})
        region_page = self.client.get(f'/plugins/dependencies/graph/?region={self.okres1.pk}').content.decode()
        self.assertIn('"overview": true', region_page)

    def test_devices_are_shown_for_a_site_a_branch_a_focus_or_when_asked(self):
        for query in (f'?site={self.s1.pk}', f'?branch={self.devs["core"].pk}', f'?focus={self.devs["core"].pk}&impact=1', '?view=devices',
                      f'?region={self.okres1.pk}&view=devices'):
            page = self.client.get('/plugins/dependencies/graph/' + query).content.decode()
            self.assertNotIn('"overview": true', page, query)
        page = self.client.get(f'/plugins/dependencies/graph/?region={self.okres1.pk}&view=devices').content.decode()
        self.assertIn(f'?region={self.okres1.pk}', page)                          # the way back to the clouds

    def test_permissions(self):
        nobody = Client()
        nobody.force_login(get_user_model().objects.create_user('nobody2', 'n2@example.com', 'x'))
        self.assertEqual(nobody.get(f'{API}/overview/').status_code, 403)
        self.assertEqual(nobody.get('/plugins/dependencies/graph/').status_code, 403)

    def test_outside_clouds_are_only_those_with_a_link_to_the_region(self):
        data = build_overview(self.okres2.pk)                     # Site 3: linked from Site 1 only
        self.assertEqual({n['data']['label'] for n in data['nodes']}, {'Site 3', 'Okres 1'})
        self.assertEqual(data['totals']['clouds'], 1)
