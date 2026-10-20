"""DB testy: python manage.py test netbox_dependencies (v NetBox kontajneri)."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from netbox_dependencies.choices import SourceChoices, StateChoices
from netbox_dependencies.models import Dependency, DeviceStatus
from netbox_dependencies.status import analyze_outages, apply_icmp_report, sync_from_zabbix
from netbox_dependencies.tests.base import Base


class DependencyCleanTests(Base):
    def test_cycle_rejected(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        bad = Dependency(upstream=self.devs['ap'], downstream=self.devs['core'])
        with self.assertRaises(ValidationError):
            bad.clean()

    def test_self_rejected(self):
        with self.assertRaises(ValidationError):
            Dependency(upstream=self.devs['ap'], downstream=self.devs['ap']).clean()


class IcmpReportTests(Base):
    def test_down_only_after_threshold(self):
        d = self.devs['ap'].pk
        apply_icmp_report(d, True)
        apply_icmp_report(d, False)
        apply_icmp_report(d, False)
        self.assertEqual(DeviceStatus.objects.get(device_id=d).state, StateChoices.UP)
        apply_icmp_report(d, False)
        self.assertEqual(DeviceStatus.objects.get(device_id=d).state, StateChoices.DOWN)
        apply_icmp_report(d, True)
        st = DeviceStatus.objects.get(device_id=d)
        self.assertEqual((st.state, st.fail_count), (StateChoices.UP, 0))

    def test_first_failures_are_unknown_not_down(self):
        d = self.devs['ap'].pk
        apply_icmp_report(d, False)
        self.assertEqual(DeviceStatus.objects.get(device_id=d).state, StateChoices.UNKNOWN)


class OutageAnalysisTests(Base):
    def set_down(self, name, source=SourceChoices.ICMP):
        now = timezone.now()
        DeviceStatus.objects.update_or_create(
            device=self.devs[name],
            defaults=dict(state=StateChoices.DOWN, source=source, last_checked=now, last_changed=now),
        )

    def test_child_of_down_parent_is_explained(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        self.set_down('core')
        self.set_down('ap')
        outages, explained = analyze_outages()
        self.assertEqual([o['device'].name for o in outages], ['core'])
        self.assertEqual(outages[0]['down_count'], 2)  # sw1 + ap
        self.assertEqual([e['device'].name for e in explained], ['ap'])

    def test_independent_outages_are_both_roots(self):
        self.dep('core', 'sw1')
        self.set_down('sw1')
        self.set_down('sw2')
        outages, explained = analyze_outages()
        self.assertEqual({o['device'].name for o in outages}, {'sw1', 'sw2'})
        self.assertEqual(explained, [])

    def test_stale_status_is_ignored(self):
        self.set_down('core')
        DeviceStatus.objects.update(last_checked=timezone.now() - timezone.timedelta(hours=2))
        self.assertEqual(analyze_outages(), ([], []))


class ZabbixSyncTests(Base):
    def test_problem_pattern_marks_down(self):
        from netbox_zabbix_status.models import ZabbixHost, ZabbixProblem
        host = ZabbixHost.objects.create(
            zabbix_hostid=1, name='sw1', device=self.devs['sw1'],
            status='enabled', last_synced=timezone.now(),
        )
        ZabbixProblem.objects.create(
            host=host, zabbix_eventid=1, name='Unavailable by ICMP ping', severity=4,
        )
        ok = ZabbixHost.objects.create(
            zabbix_hostid=2, name='sw2', device=self.devs['sw2'],
            status='enabled', last_synced=timezone.now(),
        )
        ZabbixProblem.objects.create(host=ok, zabbix_eventid=2, name='High CPU load', severity=3)
        sync_from_zabbix()
        self.assertEqual(DeviceStatus.objects.get(device=self.devs['sw1']).state, StateChoices.DOWN)
        self.assertEqual(DeviceStatus.objects.get(device=self.devs['sw2']).state, StateChoices.UP)


class TreeAndMultiAddTests(Base):
    def test_tree_groups_children_under_one_root(self):
        from netbox_dependencies.tree import build_tree
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')
        self.dep('sw1', 'ap', 'up')
        self.dep('sw2', 'ap', 'up')
        roots, stats = build_tree()
        self.assertEqual([r['device'].name for r in roots], ['core'])
        core = roots[0]
        self.assertEqual((core['direct'], core['total']), (2, 3))
        self.assertEqual([c['device'].name for c in core['children']], ['sw1', 'sw2'])
        # ap is below both switches and is marked as multi-parent
        self.assertTrue(all(c['children'][0]['multi_parent'] for c in core['children']))
        self.assertEqual(stats['multi_parent'], 1)

    def test_multi_add_creates_one_row_per_downstream(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        user = get_user_model().objects.create_superuser('t', 't@example.com', 'x')
        client = Client()
        client.force_login(user)
        data = {
            'upstream': self.devs['core'].pk,
            'downstreams': [self.devs['sw1'].pk, self.devs['sw2'].pk],
            'dependency_type': 'network',
        }
        r = client.post('/plugins/dependencies/add/', data)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Dependency.objects.filter(upstream=self.devs['core']).count(), 2)
        # submitting again does not create duplicates
        client.post('/plugins/dependencies/add/', data)
        self.assertEqual(Dependency.objects.count(), 2)
        self.assertEqual(client.get('/plugins/dependencies/tree/').status_code, 200)

    def test_multi_add_rejects_cycle_atomically(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        self.dep('sw1', 'core')
        user = get_user_model().objects.create_superuser('t2', 't2@example.com', 'x')
        client = Client()
        client.force_login(user)
        r = client.post('/plugins/dependencies/add/', {
            'upstream': self.devs['core'].pk,
            'downstreams': [self.devs['sw2'].pk, self.devs['sw1'].pk],  # sw1 -> core already exists => cycle
            'dependency_type': 'network',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Dependency.objects.count(), 1)  # nothing new was created


class GraphTests(Base):
    def test_graph_data_and_page(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from netbox_dependencies.graph_data import build_graph
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')
        self.dep('sw1', 'ap', 'up')
        self.dep('sw2', 'ap', 'up')
        g = build_graph()
        self.assertEqual(len(g['nodes']), 4)
        self.assertEqual(len(g['edges']), 4)
        # branch of sw1: core (ancestor), sw1, ap (descendant) — not sw2
        branch = build_graph(device_id=self.devs['sw1'].pk)
        self.assertEqual({n['data']['label'] for n in branch['nodes']}, {'core', 'sw1', 'ap'})
        user = get_user_model().objects.create_superuser('g', 'g@example.com', 'x')
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get('/plugins/dependencies/graph/').status_code, 200)
        self.assertEqual(
            client.get(f'/plugins/dependencies/graph/?branch={self.devs["sw1"].pk}').status_code, 200)
        pk = self.devs['sw1'].pk
        page = client.get(f'/plugins/dependencies/graph/?focus={pk}&impact=1').content.decode()
        self.assertIn('"auto_simulate": true', page)        # link from a notification: simulate and frame the outage
        self.assertIn(f'"focus_device": {pk}', page)
        self.assertIn('"auto_simulate": false', client.get(f'/plugins/dependencies/graph/?focus={pk}').content.decode())
        js = client.get('/plugins/dependencies/graph/cytoscape.js')
        self.assertEqual(js.status_code, 200)
        self.assertIn('javascript', js['Content-Type'])
        js.close()


class ScopedGraphTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from dcim.models import Region, Site
        parent = Region.objects.create(name='Okres', slug='okres')
        child = Region.objects.create(name='Obec', slug='obec', parent=parent)
        cls.site_a = Site.objects.create(name='A', slug='a', region=child)
        cls.site_b = Site.objects.create(name='B', slug='b', region=parent)
        cls.parent, cls.child = parent, child
        # core+sw1 na lokalite A, sw2+ap na lokalite B
        Device.objects.filter(name__in=['core', 'sw1']).update(site=cls.site_a)
        Device.objects.filter(name__in=['sw2', 'ap']).update(site=cls.site_b)

    def setUp(self):
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')   # prepoj A -> B
        self.dep('sw2', 'ap')

    def test_site_scope_collapses_other_site_into_cloud(self):
        from netbox_dependencies.graph_data import build_graph
        g = build_graph(site_id=self.site_a.pk)
        by_id = {n['data']['id']: n['data'] for n in g['nodes']}
        labels = {d['label'] for d in by_id.values()}
        self.assertEqual(labels, {'core', 'sw1', 'B'})          # sw2 (location B) is the cloud "B"
        cloud = by_id[f'x{self.site_b.pk}']
        self.assertTrue(cloud['cloud'] and cloud['external'])
        self.assertIn(f'site={self.site_b.pk}', cloud['map_url'])
        self.assertIn(f'focus={self.devs["sw2"].pk}', cloud['map_url'])  # a single device -> focus
        self.assertEqual(g['remote_map'], {self.devs['sw2'].pk: f'x{self.site_b.pk}'})
        edge = next(e['data'] for e in g['edges'] if e['data']['target'].startswith('x'))
        self.assertEqual(edge['elabel'], 'sw2')                  # the edge carries the name of the remote device
        self.assertEqual([n['name'] for n in g['neighbours']], ['B'])
        self.assertEqual(g['scope'], f'site:{self.site_a.pk}')

    def test_region_scope_includes_subregions(self):
        from netbox_dependencies.graph_data import build_graph
        g = build_graph(region_id=self.parent.pk)   # A (in the sub-region) and B
        self.assertEqual({n['data']['label'] for n in g['nodes']}, {'core', 'sw1', 'sw2', 'ap'})
        self.assertFalse(any(n['data']['external'] for n in g['nodes']))
        self.assertEqual(g['remote_map'], {})
        crumbs = build_graph(site_id=self.site_a.pk)['crumbs']
        self.assertEqual([c['name'] for c in crumbs], ['All', 'Okres', 'Obec'])

    def test_sidebar_counts(self):
        from netbox_dependencies.graph_data import build_sidebar
        sb = build_sidebar()
        self.assertEqual(sb['total'], 4)
        okres = sb['tree'][0]
        self.assertEqual((okres['name'], okres['count']), ('Okres', 4))
        self.assertEqual({c['name'] for c in okres['children']}, {'Obec', 'B'})

    def test_save_load_and_delete_layout(self):
        import json
        from django.contrib.auth import get_user_model
        from django.test import Client
        from netbox_dependencies.graph_data import build_graph
        user = get_user_model().objects.create_superuser('lay', 'l@example.com', 'x')
        c = Client()
        c.force_login(user)
        scope = f'site:{self.site_a.pk}'
        core = self.devs['core'].pk
        r = c.post('/plugins/dependencies/graph/layout/',
                   data=json.dumps({'scope': scope, 'positions': {f'd{core}': [10.5, 20], f'x{self.site_b.pk}': [5, 6]}}),
                   content_type='application/json')
        self.assertEqual((r.status_code, r.json()['saved']), (200, 2))
        pos = build_graph(site_id=self.site_a.pk)['positions']
        self.assertEqual(pos[f'd{core}'], [10.5, 20.0])
        self.assertEqual(pos[f'x{self.site_b.pk}'], [5.0, 6.0])
        bad = c.post('/plugins/dependencies/graph/layout/',
                     data=json.dumps({'scope': 'bogus', 'positions': {}}), content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        r = c.delete('/plugins/dependencies/graph/layout/', data=json.dumps({'scope': scope}),
                     content_type='application/json')
        self.assertEqual(r.json()['deleted'], 2)
        self.assertEqual(build_graph(site_id=self.site_a.pk)['positions'], {})
        for url in (f'/plugins/dependencies/graph/?site={self.site_a.pk}',
                    f'/plugins/dependencies/graph/?region={self.parent.pk}&focus={core}'):
            self.assertEqual(c.get(url).status_code, 200)


class LayoutCsrfTests(Base):
    def test_layout_save_works_with_csrf_enforced_and_httponly_cookie(self):
        """Regression: NetBox sets CSRF_COOKIE_HTTPONLY, so the token must be in the page, not only in the cookie."""
        import json
        import re
        from django.contrib.auth import get_user_model
        from django.test import Client
        self.dep('core', 'sw1')
        user = get_user_model().objects.create_superuser('csrf', 'c@example.com', 'x')
        c = Client(enforce_csrf_checks=True)
        c.force_login(user)
        page = c.get('/plugins/dependencies/graph/').content.decode()
        token = re.search(r'id="dep-csrf"[^>]*>\s*<input[^>]*name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
        body = json.dumps({'scope': 'all', 'positions': {f'd{self.devs["core"].pk}': [1, 2]}})
        denied = c.post('/plugins/dependencies/graph/layout/', data=body, content_type='application/json')
        self.assertEqual(denied.status_code, 403)          # without a token
        ok = c.post('/plugins/dependencies/graph/layout/', data=body, content_type='application/json',
                    HTTP_X_CSRFTOKEN=token)
        self.assertEqual((ok.status_code, ok.json()['saved']), (200, 1))


class LiveStatusTests(Base):
    def _client(self, name):
        from django.contrib.auth import get_user_model
        from django.test import Client
        c = Client()
        c.force_login(get_user_model().objects.create_superuser(name, f'{name}@example.com', 'x'))
        return c

    def test_states_endpoint_returns_states_and_roots(self):
        from netbox_dependencies.models import DeviceStatus
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        now = timezone.now()
        for name, state in (('core', 'down'), ('sw1', 'down'), ('ap', 'up')):
            DeviceStatus.objects.create(device=self.devs[name], state=state, source='icmp',
                                        detail='x', last_checked=now, last_changed=now)
        # a stale record is returned as unknown
        DeviceStatus.objects.filter(device=self.devs['ap']).update(last_checked=now - timezone.timedelta(hours=3))
        body = self._client('live').get('/api/plugins/dependencies/status/states/').json()
        self.assertEqual(body['states'][str(self.devs['core'].pk)][0], 'down')
        self.assertEqual(body['states'][str(self.devs['ap'].pk)][0], 'unknown')
        self.assertEqual([r['name'] for r in body['roots']], ['core'])   # sw1 is explained by the outage of core
        self.assertEqual(body['roots'][0]['down_count'], 2)               # sw1 + ap

    def test_graph_nodes_carry_detail_fields(self):
        from netbox_dependencies.graph_data import build_graph
        from netbox_dependencies.models import DeviceStatus
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')
        now = timezone.now()
        DeviceStatus.objects.create(device=self.devs['sw1'], state='down', source='zabbix',
                                    detail='Unavailable by ICMP ping', last_checked=now, last_changed=now)
        nodes = {n['data']['label']: n['data'] for n in build_graph()['nodes']}
        self.assertEqual((nodes['core']['ups'], nodes['core']['downs']), (0, 2))
        self.assertEqual((nodes['sw1']['ups'], nodes['sw1']['downs']), (1, 0))
        self.assertEqual((nodes['sw1']['state'], nodes['sw1']['source']), ('down', 'zabbix'))
        self.assertEqual(nodes['sw1']['detail'], 'Unavailable by ICMP ping')
        self.assertTrue(nodes['sw1']['since'])
        self.assertEqual(nodes['sw1']['role'], 'R')
        self.assertIn('/api/plugins/dependencies/status/states/', build_graph()['states_url'])
        self.assertEqual(self._client('live2').get('/plugins/dependencies/graph/').status_code, 200)


class CustomMapTests(Base):
    def _client(self, name, csrf=False):
        from django.contrib.auth import get_user_model
        from django.test import Client
        c = Client(enforce_csrf_checks=csrf)
        c.force_login(get_user_model().objects.create_superuser(name, f'{name}@example.com', 'x'))
        return c

    def test_custom_map_graph_members_clouds_and_isolated(self):
        from netbox_dependencies.graph_data import build_graph
        from netbox_dependencies.models import CustomMap
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        m = CustomMap.objects.create(name='Moja')
        m.devices.set([self.devs['sw1'], self.devs['sw2']])   # sw2 has no dependency at all
        g = build_graph(map_id=m.pk)
        by_label = {n['data']['label']: n['data'] for n in g['nodes']}
        self.assertEqual(g['scope'], f'map:{m.pk}')
        self.assertEqual(g['title'], 'Moja')
        self.assertIn('sw2', by_label)                       # an isolated member of the map is visible
        self.assertFalse(by_label['sw1']['external'])
        # core and ap are outside the map but at the same location 'S' -> one cloud
        clouds = [d for d in by_label.values() if d['cloud']]
        self.assertEqual(len(clouds), 1)
        self.assertEqual(g['custom_map']['name'], 'Moja')

    def test_layout_inherited_from_all_until_map_has_its_own(self):
        from netbox_dependencies.graph_data import build_graph
        from netbox_dependencies.models import CustomMap, NodePosition
        self.dep('core', 'sw1')
        core, sw1 = self.devs['core'].pk, self.devs['sw1'].pk
        NodePosition.objects.create(node=f'd{core}', scope='all', x=1, y=2)
        NodePosition.objects.create(node=f'd{sw1}', scope='all', x=3, y=4)
        m = CustomMap.objects.create(name='X')
        m.devices.set([self.devs['core'], self.devs['sw1']])
        g = build_graph(map_id=m.pk)
        self.assertTrue(g['inherited'])
        self.assertEqual(g['positions'][f'd{core}'], [1.0, 2.0])
        NodePosition.objects.create(node=f'd{core}', scope=f'map:{m.pk}', x=9, y=9)
        g = build_graph(map_id=m.pk)
        self.assertFalse(g['inherited'])
        self.assertEqual(list(g['positions']), [f'd{core}'])      # the map's own layout wins and is not mixed
        self.assertFalse(build_graph()['inherited'])

    def test_custom_map_layout_autosave_pages_and_cleanup(self):
        import json
        import re
        from netbox_dependencies.models import CustomMap, NodePosition
        self.dep('core', 'sw1')
        m = CustomMap.objects.create(name='Campus map')
        m.devices.set([self.devs['core'], self.devs['sw1']])
        c = self._client('mk', csrf=True)
        page = c.get(f'/plugins/dependencies/graph/?map={m.pk}').content.decode()
        token = re.search(r'id="dep-csrf"[^>]*>\s*<input[^>]*name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
        self.assertIn('data-can-save="1"', page)
        self.assertNotIn('id="dep-save"', page)          # the buttons are gone, saving is automatic
        self.assertNotIn('id="dep-newmap"', page)
        core = self.devs['core'].pk
        ok = c.post('/plugins/dependencies/graph/layout/', content_type='application/json', HTTP_X_CSRFTOKEN=token,
                    data=json.dumps({'scope': f'map:{m.pk}', 'positions': {f'd{core}': [5, 5]}}))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(NodePosition.objects.filter(scope=f'map:{m.pk}').count(), 1)
        for url in ('/plugins/dependencies/maps/', '/plugins/dependencies/maps/add/',
                    f'/plugins/dependencies/maps/{m.pk}/edit/'):
            self.assertEqual(c.get(url).status_code, 200, url)
        # deleting a map deletes its layout too
        m.delete()
        self.assertFalse(NodePosition.objects.filter(scope=f'map:{m.pk}').exists())

    def test_sidebar_lists_custom_maps(self):
        from netbox_dependencies.graph_data import build_sidebar
        from netbox_dependencies.models import CustomMap
        self.dep('core', 'sw1')
        m = CustomMap.objects.create(name='A map')
        m.devices.set([self.devs['core'], self.devs['sw1']])
        sb = build_sidebar(map_id=m.pk)
        self.assertEqual([(x['name'], x['count'], x['active']) for x in sb['custom']], [('A map', 2, True)])


class LinkMediumTests(Base):
    def _client(self, name, csrf=False):
        from django.contrib.auth import get_user_model
        from django.test import Client
        c = Client(enforce_csrf_checks=csrf)
        c.force_login(get_user_model().objects.create_superuser(name, f'{name}@example.com', 'x'))
        return c

    def test_default_medium_and_graph_edge_data(self):
        from netbox_dependencies.graph_data import build_graph
        d = self.dep('core', 'sw1')
        self.assertEqual(d.medium, 'unspecified')
        d.medium = 'radio'
        d.save()
        edge = build_graph()['edges'][0]['data']
        self.assertEqual((edge['medium'], edge['pk']), ('radio', d.pk))
        self.assertEqual((edge['description'], edge['edit_url']), ('', f'{d.get_absolute_url()}edit/'))   # for the link popup
        self.assertTrue(build_graph()['dependency_api'].endswith('/dependencies/'))

    def test_change_medium_via_api_like_the_graph_context_menu(self):
        import json
        import re
        d = self.dep('core', 'sw1')
        c = self._client('ctx', csrf=True)
        page = c.get('/plugins/dependencies/graph/').content.decode()
        token = re.search(r'id="dep-csrf"[^>]*>\s*<input[^>]*name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
        url = f'/api/plugins/dependencies/dependencies/{d.pk}/'
        ok = c.patch(url, data=json.dumps({'medium': 'optical'}), content_type='application/json',
                     HTTP_X_CSRFTOKEN=token)
        self.assertEqual(ok.status_code, 200, ok.content)
        d.refresh_from_db()
        self.assertEqual(d.medium, 'optical')
        self.assertEqual(c.get(url).json()['medium'], 'optical')
        for medium in ('vpn', 'lte', 'satellite'):          # the newer media are accepted too
            r = c.patch(url, data=json.dumps({'medium': medium}), content_type='application/json', HTTP_X_CSRFTOKEN=token)
            self.assertEqual((r.status_code, r.json()['medium']['value'] if isinstance(r.json()['medium'], dict) else r.json()['medium']),
                             (200, medium))
        bad = c.patch(url, data=json.dumps({'medium': 'laser'}), content_type='application/json',
                      HTTP_X_CSRFTOKEN=token)
        self.assertEqual(bad.status_code, 400)
        denied = c.patch(url, data=json.dumps({'medium': 'radio'}), content_type='application/json')
        self.assertEqual(denied.status_code, 403)       # without a CSRF token the change is not allowed

    def test_legend_lives_in_modal_not_in_the_graph_card(self):
        page = self._client('leg').get('/plugins/dependencies/graph/').content.decode()
        self.assertIn('id="dep-legend-modal"', page)
        self.assertIn('data-bs-target="#dep-legend-modal"', page)
        self.assertNotIn('dep-legend">', page)             # the original legend strip above the graph is gone
        self.assertIn('Optical', page)

    def test_multi_add_sets_medium_and_filter_works(self):
        from netbox_dependencies.filtersets import DependencyFilterSet
        from netbox_dependencies.models import Dependency
        c = self._client('mm')
        r = c.post('/plugins/dependencies/add/', {
            'upstream': self.devs['core'].pk, 'downstreams': [self.devs['sw1'].pk],
            'dependency_type': 'network', 'medium': 'radio'})
        self.assertEqual(r.status_code, 302)
        r = c.post('/plugins/dependencies/add/', {
            'upstream': self.devs['core'].pk, 'downstreams': [self.devs['sw2'].pk], 'dependency_type': 'network'})
        self.assertEqual(r.status_code, 302)
        by = {d.downstream.name: d.medium for d in Dependency.objects.all()}
        self.assertEqual(by, {'sw1': 'radio', 'sw2': 'unspecified'})
        qs = DependencyFilterSet({'medium': ['radio']}, Dependency.objects.all()).qs
        self.assertEqual([d.downstream.name for d in qs], ['sw1'])


class DeviceInfoTests(Base):
    def test_graph_node_carries_device_info(self):
        from netbox_dependencies.graph_data import build_graph
        self.dep('core', 'sw1')
        Device.objects.filter(pk=self.devs['sw1'].pk).update(description='Core uplink', serial='SN123')
        Device.objects.filter(pk=self.devs['core'].pk).update(comments='x' * 1500)
        nodes = {n['data']['label']: n['data'] for n in build_graph()['nodes']}
        self.assertEqual((nodes['sw1']['description'], nodes['sw1']['serial']), ('Core uplink', 'SN123'))
        self.assertEqual(nodes['sw1']['comments'], '')
        self.assertFalse(nodes['sw1']['comments_more'])
        self.assertEqual(len(nodes['core']['comments']), 1000)
        self.assertTrue(nodes['core']['comments_more'])
        self.assertEqual(nodes['core']['location'], '')


class ZabbixWatchdogTests(TestCase):
    def _job(self, status, started_ago=None, completed_ago=None, name='Zabbix sync'):
        from core.models import Job
        now = timezone.now()
        import uuid
        return Job.objects.create(
            name=name, status=status, interval=5, job_id=uuid.uuid4(),
            started=now - started_ago if started_ago else None,
            completed=now - completed_ago if completed_ago else None,
        )

    def _heal(self):
        from unittest import mock
        from netbox_dependencies.watchdog import heal_zabbix_sync
        # enqueue_once would put a real RQ job into the live Redis — it is intercepted in the test
        with mock.patch('netbox_zabbix_status.jobs.ZabbixSyncJob.enqueue_once') as enq:
            return heal_zabbix_sync(), enq

    def test_stuck_running_job_is_failed_and_sync_rescheduled(self):
        from core.models import Job
        stuck = self._job('running', started_ago=timezone.timedelta(hours=1))
        result, enq = self._heal()
        stuck.refresh_from_db()
        self.assertEqual(stuck.status, 'failed')
        self.assertIn('watchdog', stuck.error)
        self.assertEqual(result, {'failed_stale': [stuck.pk], 'rescheduled': True})
        enq.assert_called_once_with(interval=5)
        self.assertEqual(Job.objects.filter(name='Zabbix sync', status='running').count(), 0)

    def test_healthy_states_are_left_alone(self):
        recent = self._job('running', started_ago=timezone.timedelta(minutes=2))   # running right now
        result, enq = self._heal()
        recent.refresh_from_db()
        self.assertEqual(recent.status, 'running')
        self.assertEqual(result, {'failed_stale': [], 'rescheduled': False})
        enq.assert_not_called()

    def test_scheduled_successor_is_not_duplicated(self):
        self._job('scheduled')
        result, enq = self._heal()
        self.assertFalse(result['rescheduled'])
        enq.assert_not_called()

    def test_reschedules_when_nothing_is_enqueued_but_not_right_after_a_run(self):
        done = self._job('completed', started_ago=timezone.timedelta(minutes=10), completed_ago=timezone.timedelta(minutes=9))
        result, enq = self._heal()
        self.assertTrue(result['rescheduled'])                 # nothing is scheduled -> it gets scheduled
        enq.assert_called_once()
        done.completed = timezone.now() - timezone.timedelta(seconds=10)
        done.save()
        result, enq = self._heal()
        self.assertFalse(result['rescheduled'])                # just finished: the next run schedules itself

    def test_other_jobs_are_not_touched(self):
        snmp = self._job('running', started_ago=timezone.timedelta(days=3), name='SNMP Sync')
        self._heal()
        snmp.refresh_from_db()
        self.assertEqual(snmp.status, 'running')


class PaginationAndLimitsTests(Base):
    def _client(self, name):
        from django.contrib.auth import get_user_model
        from django.test import Client
        c = Client()
        c.force_login(get_user_model().objects.create_superuser(name, f'{name}@example.com', 'x'))
        return c

    def _clones(self, prefix, count):
        base = self.devs['core']
        return [Device.objects.create(name=f'{prefix}{i:02d}', site=base.site, device_type=base.device_type, role=base.role)
                for i in range(count)]

    def test_tree_paginates_roots_and_builds_only_one_page(self):
        from netbox_dependencies.tree import build_tree
        roots = self._clones('r', 12)
        kids = self._clones('k', 12)
        for r, k in zip(roots, kids):
            Dependency.objects.create(upstream=r, downstream=k)
        all_roots, stats = build_tree()
        self.assertEqual((len(all_roots), stats['roots'], stats['page']), (12, 12, None))
        page_roots, stats = build_tree(page_number=1, per_page=2)
        self.assertEqual(len(page_roots), 2)                      # only one page was built
        self.assertEqual((stats['roots'], stats['devices']), (12, 24))   # the totals are for the whole tree
        self.assertGreater(stats['page'].paginator.num_pages, 1)
        later, _ = build_tree(page_number=2, per_page=2)
        self.assertNotEqual({n['device'].pk for n in page_roots}, {n['device'].pk for n in later})
        junk, _ = build_tree(page_number='abc', per_page=2)       # an invalid page does not crash
        self.assertEqual(len(junk), 2)
        page = self._client('tr').get('/plugins/dependencies/tree/?per_page=2')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Showing 1-2 of 12', page.content.decode())

    def test_device_tab_limits_lists_and_paginates_impact(self):
        from netbox_dependencies.views import TAB_LIST_LIMIT
        children = self._clones('c', 30)
        for c in children:
            Dependency.objects.create(upstream=self.devs['core'], downstream=c)
        url = f'/dcim/devices/{self.devs["core"].pk}/dependencies/'
        html = self._client('tab').get(url + '?per_page=10').content.decode()
        self.assertIn(f'Showing {TAB_LIST_LIMIT} of 30', html)
        self.assertIn(f'dependency_list'.replace('dependency_list', '/plugins/dependencies/dependencies/') + f'?upstream_id={self.devs["core"].pk}', html)
        self.assertIn('Showing 1-10 of 30', html)                 # the impact table is paginated
        self.assertEqual(html.count('/dcim/devices/') > 0, True)
        self.assertLess(html.count('<tr>'), 80)                   # not all 30+30 rows at once

    def test_impact_page_paginates(self):
        children = self._clones('c', 30)
        for c in children:
            Dependency.objects.create(upstream=self.devs['core'], downstream=c)
        html = self._client('imp').get(f'/plugins/dependencies/impact/?devices={self.devs["core"].pk}&per_page=10').content.decode()
        self.assertIn('Showing 1-10 of 30', html)
        self.assertIn('30 down', html)                            # the summary covers the whole result, not a page

    def test_live_outages_paginates_explained_list(self):
        now = timezone.now()
        kids = self._clones('e', 12)
        for k in kids:
            Dependency.objects.create(upstream=self.devs['core'], downstream=k)
        for d in [self.devs['core'], *kids]:
            DeviceStatus.objects.create(device=d, state='down', source='icmp', detail='x',
                                        last_checked=now, last_changed=now - timezone.timedelta(minutes=10))
        html = self._client('lv').get('/plugins/dependencies/live/?per_page=5').content.decode()
        self.assertIn('Showing 1-5 of 12', html)
        self.assertIn('>12<', html)                               # the number of explained devices is for the whole list

    def test_graph_over_the_node_limit_is_not_drawn_unless_forced(self):
        from unittest import mock
        kids = self._clones('g', 6)
        for k in kids:
            Dependency.objects.create(upstream=self.devs['core'], downstream=k)
        c = self._client('gr')
        real = __import__('netbox.plugins', fromlist=['get_plugin_config']).get_plugin_config
        limited = lambda plugin, key: 3 if key == 'graph_max_nodes' else real(plugin, key)
        with mock.patch('netbox_dependencies.views.get_plugin_config', side_effect=limited):
            page = c.get('/plugins/dependencies/graph/').content.decode()
            self.assertIn('"too_large": {"nodes": 7, "limit": 3', page)
            self.assertIn('"nodes": []', page)
            forced = c.get('/plugins/dependencies/graph/?force=1').content.decode()
            self.assertIn('"too_large": null', forced)
            self.assertNotIn('"nodes": []', forced)

    def test_api_caps_large_answers(self):
        children = self._clones('a', 12)
        for ch in children:
            Dependency.objects.create(upstream=self.devs['core'], downstream=ch)
        c = self._client('api')
        body = c.get(f'/api/plugins/dependencies/impact/?device={self.devs["core"].pk}&limit=5').json()
        self.assertEqual((len(body['affected']), body['affected_total'], body['truncated']), (5, 12, True))
        self.assertEqual(body['summary']['down'], 12)             # the summary is not truncated
        full = c.get(f'/api/plugins/dependencies/impact/?device={self.devs["core"].pk}').json()
        self.assertEqual((len(full['affected']), full['truncated']), (12, False))
        bad = c.get(f'/api/plugins/dependencies/impact/?device={self.devs["core"].pk}&limit=x').json()
        self.assertEqual(len(bad['affected']), 12)                # an invalid limit = the default
        out = c.get('/api/plugins/dependencies/outages/').json()
        self.assertIn('root_causes_total', out)

    def test_old_notification_records_are_purged(self):
        from unittest import mock
        from netbox_dependencies import notifications as n
        from netbox_dependencies.models import OutageNotification
        now = timezone.now()
        old = OutageNotification.objects.create(device=self.devs['core'], started_at=now - timezone.timedelta(days=200),
                                                resolved_at=now - timezone.timedelta(days=100))
        recent = OutageNotification.objects.create(device=self.devs['core'], started_at=now - timezone.timedelta(days=5),
                                                   resolved_at=now - timezone.timedelta(days=2))
        ongoing = OutageNotification.objects.create(device=self.devs['sw1'], started_at=now - timezone.timedelta(days=300))
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda p, k: 0):
            self.assertEqual(n.purge_old_notifications(), 0)      # 0 = never delete
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda p, k: 90):
            self.assertEqual(n.purge_old_notifications(), 1)
        self.assertEqual(set(OutageNotification.objects.values_list('pk', flat=True)), {recent.pk, ongoing.pk})
        self.assertNotIn(old.pk, OutageNotification.objects.values_list('pk', flat=True))


class IconTests(Base):
    def test_icon_is_chosen_from_role_then_model_then_name(self):
        from netbox_dependencies.icons import graph_icons, icon_for
        self.assertEqual(icon_for('Firewall', 'whatever', 'sw-1'), 'firewall')       # the role wins
        self.assertEqual(icon_for('Generic', 'CCR2004 router', 'sw-1'), 'router')    # then the model
        self.assertEqual(icon_for('', '', 'cam-gate-01'), 'camera')                  # then the name
        self.assertEqual(icon_for('Generic', 'Model', 'thing'), '')                   # nothing matches: no icon
        icons = graph_icons()
        self.assertEqual({r['key'] for r in icons['rules']}, set(icons['paths']))     # every rule has a path

    def test_tree_shows_status_discs_chips_and_the_graph_gets_the_icon_set(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        from netbox_dependencies.graph_data import build_graph
        from netbox_dependencies.models import DeviceStatus
        dep = self.dep('core', 'sw1', group='g1')
        dep.medium = 'vpn'
        dep.save()
        now = timezone.now()
        DeviceStatus.objects.create(device=self.devs['sw1'], state='down', source='icmp', last_checked=now, last_changed=now)
        client = Client()
        client.force_login(get_user_model().objects.create_superuser('t', 't@example.com', 'x'))
        page = client.get('/plugins/dependencies/tree/?expanded=1').content.decode()
        self.assertIn('dep-disc-down', page)
        self.assertIn('VPN / tunnel', page)
        self.assertIn('g1', page)
        self.assertIn('is-down', page)
        self.assertIn('paths', build_graph()['icons'])


class ExportForImportTests(Base):
    def _client(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        client = Client()
        client.force_login(get_user_model().objects.create_superuser('ex', 'ex@example.com', 'x'))
        return client

    def _make(self):
        from extras.models import Tag
        first = self.dep('core', 'sw1', group='uplink')
        first.medium, first.description = 'optical', 'Main uplink, "fiber"'
        first.save()
        first.tags.add(Tag.objects.create(name='Critical', slug='critical'), Tag.objects.create(name='Core', slug='core-tag'))
        second = self.dep('core', 'sw2')
        second.medium = 'radio'
        second.save()
        self.dep('sw1', 'ap')

    @staticmethod
    def _snapshot():
        return sorted((d.upstream.name, d.downstream.name, d.dependency_type, d.medium, d.redundancy_group, d.description,
                       tuple(sorted(t.slug for t in d.tags.all()))) for d in Dependency.objects.all())

    def test_exported_csv_can_be_imported_again(self):
        client = self._client()
        self._make()
        before = self._snapshot()
        response = client.get('/plugins/dependencies/dependencies/export-for-import/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        text = response.content.decode('utf-8')
        self.assertEqual(text.splitlines()[0], 'upstream,downstream,dependency_type,medium,redundancy_group,description,tags')
        Dependency.objects.all().delete()
        imported = client.post('/plugins/dependencies/dependencies/import/', {'data': text, 'format': 'csv', 'csv_delimiter': ','})
        self.assertEqual(imported.status_code, 302, imported.content.decode()[:500])
        self.assertEqual(self._snapshot(), before)

    def test_export_follows_the_list_filters(self):
        client = self._client()
        self._make()
        text = client.get('/plugins/dependencies/dependencies/export-for-import/?medium=radio').content.decode('utf-8')
        self.assertEqual(len(text.strip().splitlines()), 2)         # the header and the one radio link
        self.assertIn('sw2', text)

    def test_button_on_the_list_and_permission(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        page = self._client().get('/plugins/dependencies/dependencies/').content.decode()
        self.assertIn('Re-importable CSV', page)                  # an entry of the Export dropdown
        self.assertIn('Current View', page)                       # the standard entries are still there
        self.assertIn('/dependencies/export-for-import/', page)
        nobody = Client()
        nobody.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        self.assertEqual(nobody.get('/plugins/dependencies/dependencies/export-for-import/').status_code, 403)


class CustomMapDeleteAndApiTests(Base):
    """Regression: deleting a custom map failed with SerializerNotFound because the model had no API serializer."""

    def _client(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        client = Client()
        client.force_login(get_user_model().objects.create_superuser('cm', 'cm@example.com', 'x'))
        return client

    def test_bulk_and_single_delete_of_custom_maps(self):
        from netbox_dependencies.models import CustomMap
        client = self._client()
        first, second, third = (CustomMap.objects.create(name=n) for n in ('one', 'two', 'three'))
        # the list page posts the selected maps with "confirm" (first the confirmation page, then the deletion)
        r = client.post('/plugins/dependencies/maps/delete/', {'pk': [first.pk, second.pk], 'confirm': True, '_confirm': True})
        self.assertIn(r.status_code, (200, 302), r.content[:300])
        self.assertEqual(list(CustomMap.objects.values_list('name', flat=True)), ['three'])
        r = client.post(f'/plugins/dependencies/maps/{third.pk}/delete/', {'confirm': True})
        self.assertIn(r.status_code, (200, 302))
        self.assertFalse(CustomMap.objects.exists())

    def test_custom_maps_have_an_api(self):
        from netbox_dependencies.models import CustomMap
        client = self._client()
        m = CustomMap.objects.create(name='api map')
        m.devices.set([self.devs['core']])
        body = client.get('/api/plugins/dependencies/maps/').json()
        self.assertEqual((body['count'], body['results'][0]['name']), (1, 'api map'))
        self.assertEqual([d['name'] for d in body['results'][0]['devices']], ['core'])


class AddShortcutTests(Base):
    """"Add dependents" / "Add parent" in the graph: the add page gets the device filled in and returns to the graph."""

    ADD = '/plugins/dependencies/add/'

    def _client(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        client = Client()
        client.force_login(get_user_model().objects.create_superuser('sc', 'sc@example.com', 'x'))
        return client

    def test_graph_page_offers_the_add_url_only_to_those_who_may_add(self):
        from django.contrib.auth import get_user_model
        from django.test import Client
        self.dep('core', 'sw1')
        self.assertIn('"add_url": "/plugins/dependencies/add/"', self._client().get('/plugins/dependencies/graph/').content.decode())
        viewer = Client()
        user = get_user_model().objects.create_user('viewer', 'v@example.com', 'x')
        from django.contrib.auth.models import Permission
        from users.models import ObjectPermission
        permission = ObjectPermission.objects.create(name='view deps', actions=['view'])
        from django.contrib.contenttypes.models import ContentType
        permission.object_types.set([ContentType.objects.get_for_model(Dependency)])
        permission.users.add(user)
        viewer.force_login(user)
        page = viewer.get('/plugins/dependencies/graph/').content.decode()
        self.assertIn('"add_url": ""', page)

    def test_dependents_prefilled_and_redirect_back(self):
        client = self._client()
        page = client.get(f'{self.ADD}?upstream={self.devs["core"].pk}&return_url=/plugins/dependencies/graph/%3Fsite%3D1').content.decode()
        import re
        self.assertTrue(re.search(rf'<option value="{self.devs["core"].pk}"\s+selected', page), 'the upstream device is pre-selected')
        self.assertIn('name="return_url"', page)
        r = client.post(self.ADD, {'upstream': self.devs['core'].pk, 'downstreams': [self.devs['sw1'].pk, self.devs['sw2'].pk],
                                   'dependency_type': 'network', 'medium': 'ethernet', 'direction': 'children',
                                   'return_url': '/plugins/dependencies/graph/?site=1'})
        self.assertEqual((r.status_code, r['Location']), (302, '/plugins/dependencies/graph/?site=1'))
        self.assertEqual(Dependency.objects.filter(upstream=self.devs['core']).count(), 2)

    def test_parents_direction(self):
        client = self._client()
        page = client.get(f'{self.ADD}?direction=parents&downstream={self.devs["ap"].pk}').content.decode()
        self.assertIn('name="upstreams"', page)
        self.assertNotIn('name="downstreams"', page)
        r = client.post(self.ADD, {'downstream': self.devs['ap'].pk, 'upstreams': [self.devs['sw1'].pk, self.devs['sw2'].pk],
                                   'dependency_type': 'network', 'medium': 'radio', 'redundancy_group': 'g', 'direction': 'parents'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(sorted(Dependency.objects.filter(downstream=self.devs['ap']).values_list('upstream__name', 'medium')),
                         [('sw1', 'radio'), ('sw2', 'radio')])

    def test_cycles_duplicates_and_foreign_return_urls_are_handled(self):
        client = self._client()
        self.dep('core', 'sw1')
        base = {'dependency_type': 'network', 'medium': 'ethernet', 'direction': 'parents'}
        # sw1 already depends on core: skipped, not an error
        r = client.post(self.ADD, {**base, 'downstream': self.devs['sw1'].pk, 'upstreams': [self.devs['core'].pk]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Dependency.objects.count(), 1)
        # core would depend on sw1, which depends on core: a cycle is rejected and nothing is created
        r = client.post(self.ADD, {**base, 'downstream': self.devs['core'].pk, 'upstreams': [self.devs['sw1'].pk]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Dependency.objects.count(), 1)
        # a return_url pointing to another site is ignored
        r = client.post(self.ADD, {**base, 'downstream': self.devs['ap'].pk, 'upstreams': [self.devs['sw2'].pk],
                                   'return_url': 'https://evil.example/phish'})
        self.assertEqual((r.status_code, r['Location']), (302, '/plugins/dependencies/tree/'))


class ParentLayoutTests(Base):
    """A region map without a layout of its own is composed from the layouts of its sub-maps."""

    def setUp(self):
        from dcim.models import Region, Site
        self.region = Region.objects.create(name='Novaky', slug='novaky')
        self.site_a = Site.objects.create(name='Novaky (A)', slug='na', region=self.region)
        self.site_b = Site.objects.create(name='Novaky (B)', slug='nb', region=self.region)
        for name, site in (('core', self.site_a), ('sw1', self.site_a), ('sw2', self.site_b), ('ap', self.site_b)):
            self.devs[name].site = site
            self.devs[name].save()
        self.dep('core', 'sw1')
        self.dep('sw2', 'ap')
        self.dep('sw1', 'sw2')                     # a link between the two sites

    def _save(self, scope, **positions):
        from netbox_dependencies.models import NodePosition
        for name, (x, y) in positions.items():
            NodePosition.objects.create(scope=scope, node=f'd{self.devs[name].pk}', x=x, y=y)

    def test_region_takes_over_the_layouts_of_its_sites(self):
        from netbox_dependencies.graph_data import build_graph
        self._save(f'site:{self.site_a.pk}', core=(100, 100), sw1=(300, 100))
        self._save(f'site:{self.site_b.pk}', sw2=(100, 100), ap=(100, 300))      # the same origin as the other site
        graph = build_graph(region_id=self.region.pk)
        pos = graph['positions']
        key = lambda n: f'd{self.devs[n].pk}'
        self.assertEqual((graph['inherited'], graph['inherited_from']), (True, 'children'))
        self.assertEqual(pos[key('sw1')][0] - pos[key('core')][0], 200)         # site A keeps its arrangement
        self.assertEqual(pos[key('ap')][1] - pos[key('sw2')][1], 200)           # site B keeps its arrangement
        self.assertGreater(min(pos[key('sw2')][0], pos[key('ap')][0]), max(pos[key('core')][0], pos[key('sw1')][0]))   # side by side

    def test_own_layout_wins_and_devices_without_a_layout_go_below(self):
        from netbox_dependencies.graph_data import build_graph
        self._save(f'site:{self.site_a.pk}', core=(0, 0), sw1=(200, 0))          # site B has no layout at all
        graph = build_graph(region_id=self.region.pk)
        pos = graph['positions']
        self.assertEqual(len([k for k in pos if k.startswith('d')]), 4)          # every device has a position
        self.assertGreater(pos[f'd{self.devs["sw2"].pk}'][1], pos[f'd{self.devs["core"].pk}'][1])
        self._save(f'region:{self.region.pk}', core=(5, 5))
        own = build_graph(region_id=self.region.pk)
        self.assertEqual((own['inherited'], list(own['positions'].values())), (False, [[5, 5]]))

    def test_nested_regions_and_the_all_map(self):
        from dcim.models import Region
        from netbox_dependencies.graph_data import build_graph
        inner = Region.objects.create(name='Inner', slug='inner', parent=self.region)
        self.site_b.region = inner
        self.site_b.save()
        self._save(f'site:{self.site_a.pk}', core=(0, 0), sw1=(150, 0))
        self._save(f'site:{self.site_b.pk}', sw2=(0, 0), ap=(0, 120))
        for graph in (build_graph(region_id=self.region.pk), build_graph()):
            self.assertEqual(graph['inherited_from'], 'children')
            self.assertEqual(len([k for k in graph['positions'] if k.startswith('d')]), 4)

    def test_without_any_sub_layout_the_all_map_is_still_the_fallback(self):
        from netbox_dependencies.graph_data import build_graph
        self._save('all', core=(1, 1), sw1=(2, 2), sw2=(3, 3), ap=(4, 4))
        graph = build_graph(region_id=self.region.pk)
        self.assertEqual((graph['inherited'], graph['inherited_from']), (True, 'all'))
