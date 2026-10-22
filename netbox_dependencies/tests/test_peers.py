"""Peer links: a link between equal devices - drawn without an arrow, no effect on the outage analysis."""
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone

from netbox_dependencies.graph_data import build_graph, build_sidebar
from netbox_dependencies.impact import get_impact
from netbox_dependencies.models import DeviceStatus, Dependency, PeerLink
from netbox_dependencies.status import analyze_outages
from netbox_dependencies.tests.base import Base
from netbox_dependencies.tree import build_tree

API = '/api/plugins/dependencies'


class PeerModelTests(Base):
    def peer(self, a, b, **kwargs):
        link = PeerLink(device_a=self.devs[a], device_b=self.devs[b], **kwargs)
        link.full_clean()
        link.save()
        return link

    def test_the_pair_is_stored_in_a_fixed_order_whichever_way_it_is_entered(self):
        first = self.peer('sw2', 'sw1')
        self.assertLess(first.device_a_id, first.device_b_id)
        self.assertEqual(str(first), f'{first.device_a} ↔ {first.device_b}')
        with self.assertRaises(ValidationError):
            self.peer('sw1', 'sw2')                                     # the same pair the other way round
        self.assertEqual(PeerLink.between(self.devs['sw1'].pk, self.devs['sw2'].pk).count(), 1)

    def test_self_link_and_a_pair_that_already_has_a_dependency_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.peer('sw1', 'sw1')
        self.dep('core', 'sw1')
        with self.assertRaises(ValidationError):
            self.peer('sw1', 'core')                                    # a pair is either a dependency or a peer link
        self.peer('sw1', 'sw2')
        with self.assertRaises(ValidationError):
            dep = Dependency(upstream=self.devs['sw2'], downstream=self.devs['sw1'])
            dep.full_clean()                                           # ... and the other way round

    def test_the_database_enforces_the_order_and_uniqueness(self):
        low, high = sorted((self.devs['sw1'], self.devs['sw2']), key=lambda d: d.pk)
        PeerLink.objects.create(device_a=low, device_b=high)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PeerLink.objects.bulk_create([PeerLink(device_a=low, device_b=high)])
        with self.assertRaises(IntegrityError), transaction.atomic():
            PeerLink.objects.bulk_create([PeerLink(device_a=high, device_b=low)])       # bypasses save(): the check constraint stops it


class PeerEffectTests(Base):
    def test_peer_links_do_not_change_the_impact_or_the_outages(self):
        self.dep('core', 'sw1')
        PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'])
        rows, summary = get_impact([self.devs['sw1'].pk])
        self.assertEqual((summary['down'], summary['degraded']), (0, 0))                   # sw2 is not affected by sw1
        now = timezone.now()
        DeviceStatus.objects.create(device=self.devs['sw1'], state='down', source='icmp', last_checked=now, last_changed=now)
        outages, explained = analyze_outages()
        self.assertEqual([o['device'].name for o in outages], ['sw1'])
        self.assertEqual(explained, [])

    def test_graph_draws_a_peer_link_as_an_edge_and_counts_it(self):
        self.dep('core', 'sw1')
        link = PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'], medium='optical', description='trunk')
        graph = build_graph()
        peers = [e['data'] for e in graph['edges'] if e['data']['peer']]
        self.assertEqual(len(peers), 1)
        edge = peers[0]
        self.assertEqual((edge['id'], edge['type'], edge['medium'], edge['description']), (f'p{link.pk}', 'peer', 'optical', 'trunk'))
        self.assertEqual(sorted((edge['source'], edge['target'])), sorted((f'd{self.devs["sw1"].pk}', f'd{self.devs["sw2"].pk}')))
        self.assertTrue(edge['edit_url'].endswith(f'/peers/{link.pk}/edit/'))
        self.assertFalse(any(e['data']['peer'] for e in graph['edges'] if e['data']['id'].startswith('e')))
        nodes = {n['data']['label']: n['data'] for n in graph['nodes']}
        self.assertEqual((nodes['sw1']['peers'], nodes['sw2']['peers'], nodes['core']['peers']), (1, 1, 0))
        self.assertIn('peer_api', graph)
        # a device with nothing but a peer link is on the map, in the sidebar and in the tree
        self.assertEqual(set(nodes), {'core', 'sw1', 'sw2'})
        self.assertEqual(build_sidebar()['total'], 3)
        roots, _stats = build_tree()
        tree = {r['device'].name: r for r in roots}
        self.assertEqual(tree['sw2']['peers'], ['sw1'])
        self.assertEqual(tree['core']['children'][0]['peers'], ['sw2'])

    def test_a_peer_link_to_another_location_becomes_a_cloud_link(self):
        from dcim.models import Site
        other = Site.objects.create(name='Other', slug='other')
        self.devs['sw2'].site = other
        self.devs['sw2'].save()
        PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'])
        graph = build_graph(site_id=self.devs['sw1'].site_id)
        edge = next(e['data'] for e in graph['edges'] if e['data']['peer'])
        self.assertIn(f'x{other.pk}', (edge['source'], edge['target']))                # drawn to the cloud of the other location
        self.assertEqual(edge['elabel'], 'sw2')


class PeerPagesTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('pp', 'pp@example.com', 'x'))

    def test_api_creates_orders_and_validates(self):
        body = {'device_a': self.devs['sw2'].pk, 'device_b': self.devs['sw1'].pk, 'medium': 'ethernet', 'description': 'x'}
        r = self.client.post(f'{API}/peers/', data=json.dumps(body), content_type='application/json')
        self.assertEqual(r.status_code, 201, r.content)
        created = r.json()
        self.assertLess(created['device_a']['id'], created['device_b']['id'])
        medium = created['medium']
        self.assertEqual(medium['value'] if isinstance(medium, dict) else medium, 'ethernet')
        again = self.client.post(f'{API}/peers/', data=json.dumps({**body, 'device_a': self.devs['sw1'].pk, 'device_b': self.devs['sw2'].pk}),
                                 content_type='application/json')
        self.assertEqual(again.status_code, 400)                                        # the same pair
        self.dep('core', 'sw1')
        r = self.client.post(f'{API}/peers/', data=json.dumps({'device_a': self.devs['core'].pk, 'device_b': self.devs['sw1'].pk}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400)                                            # they already have a dependency
        self.assertEqual(self.client.get(f'{API}/peers/?device_id={self.devs["sw1"].pk}').json()['count'], 1)
        self.assertEqual(self.client.delete(f'{API}/peers/{created["id"]}/').status_code, 204)

    def test_dependency_api_refuses_a_pair_that_is_linked_as_peers(self):
        PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'])
        r = self.client.post(f'{API}/dependencies/', data=json.dumps({'upstream': self.devs['sw1'].pk, 'downstream': self.devs['sw2'].pk}),
                             content_type='application/json')
        self.assertEqual(r.status_code, 400, r.content)

    def test_ui_list_detail_edit_and_menu_page(self):
        link = PeerLink.objects.create(device_a=self.devs['sw1'], device_b=self.devs['sw2'], description='trunk between the aggregation switches')
        listing = self.client.get('/plugins/dependencies/peers/').content.decode()
        self.assertIn('trunk between the aggregation switches', listing)
        detail = self.client.get(f'/plugins/dependencies/peers/{link.pk}/').content.decode()
        self.assertIn('Equal devices', detail)
        self.assertEqual(self.client.get(f'/plugins/dependencies/peers/{link.pk}/edit/').status_code, 200)
        self.assertEqual(self.client.get('/plugins/dependencies/peers/add/').status_code, 200)
        r = self.client.post('/plugins/dependencies/peers/add/', {
            'device_a': self.devs['core'].pk, 'device_b': self.devs['ap'].pk, 'medium': 'radio', 'description': 'new'})
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(PeerLink.objects.count(), 2)

    def test_csv_import_and_the_tree_shows_peers(self):
        text = 'device_a,device_b,medium,description\nsw1,sw2,optical,trunk\n'
        r = self.client.post('/plugins/dependencies/peers/import/', {'data': text, 'format': 'csv', 'csv_delimiter': ','})
        self.assertEqual(r.status_code, 302, r.content[:300])
        link = PeerLink.objects.get()
        self.assertEqual((link.medium, link.description), ('optical', 'trunk'))
        self.dep('core', 'sw1')
        tree = self.client.get('/plugins/dependencies/tree/?expanded=1').content.decode()
        self.assertIn('dep-chip dep-chip-peer', tree)
        self.assertIn('sw2', tree)
