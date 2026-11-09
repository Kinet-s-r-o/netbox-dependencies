"""Several links between a device and the same cloud are merged into one line."""
from dcim.models import Site
from netbox_dependencies.graph_data import build_graph
from netbox_dependencies.models import PeerLink
from netbox_dependencies.tests.base import Base


class CloudLinkTests(Base):
    def setUp(self):
        self.remote = Site.objects.create(name='Remote', slug='remote')
        for name in ('sw1', 'sw2', 'ap', 'x'):
            self.devs[name].site = self.remote
            self.devs[name].save()

    def edges(self, site):
        return {e['data']['id']: e['data'] for e in build_graph(site_id=site.pk)['edges']}

    def test_three_links_to_one_cloud_are_one_line(self):
        for name in ('sw1', 'sw2', 'ap'):
            self.dep('core', name)
        edges = self.edges(self.devs['core'].site)
        self.assertEqual(len(edges), 1)
        line = next(iter(edges.values()))
        self.assertEqual((line['merged'], line['count'], line['elabel'], line['peer']), (True, 3, '3 links', False))
        self.assertEqual(sorted(i['to_name'] for i in line['items']), ['ap', 'sw1', 'sw2'])
        self.assertTrue(all(i['from_id'] == self.devs['core'].pk and i['pk'] and i['url'] for i in line['items']))
        self.assertTrue(line['source'].startswith('d') and line['target'].startswith('x'))

    def test_a_single_link_stays_and_so_do_opposite_directions(self):
        self.dep('core', 'sw1')
        self.dep('sw2', 'core')
        edges = self.edges(self.devs['core'].site)
        self.assertEqual(len(edges), 2)
        self.assertFalse(any(e.get('merged') for e in edges.values()))

    def test_peer_links_are_merged_separately_from_dependencies(self):
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')
        PeerLink.objects.create(device_a=self.devs['core'], device_b=self.devs['ap'])
        PeerLink.objects.create(device_a=self.devs['core'], device_b=self.devs['x'])
        lines = list(self.edges(self.devs['core'].site).values())
        self.assertEqual(sorted((e['peer'], e['count']) for e in lines), [(False, 2), (True, 2)])

    def test_links_inside_a_map_are_not_merged(self):
        self.dep('sw1', 'sw2')
        self.dep('sw1', 'ap')
        edges = build_graph(site_id=self.remote.pk)['edges']
        self.assertEqual(len(edges), 2)
        self.assertFalse(any(e['data'].get('merged') for e in edges))
