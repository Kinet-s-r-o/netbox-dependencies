"""Suppressed devices: out of the outage analysis for a while (backup units, maintenance)."""
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from netbox_dependencies.graph_data import build_graph
from netbox_dependencies.models import DeviceStatus, DeviceSuppression, OutageNotification
from netbox_dependencies.status import analyze_outages, down_device_ids, suppressed_info
from netbox_dependencies.tests.base import Base
from netbox_dependencies.tests.test_notifications import NotifBase
from netbox_dependencies import notifications as n


def set_status(device, state, minutes_ago=10):
    now = timezone.now()
    DeviceStatus.objects.update_or_create(
        device=device, defaults=dict(state=state, source='icmp', detail='', last_checked=now,
                                     last_changed=now - timedelta(minutes=minutes_ago)))


class SuppressionLogicTests(Base):
    def suppress(self, name, **kwargs):
        return DeviceSuppression.objects.create(device=self.devs[name], **kwargs)

    def test_a_suppressed_down_device_is_not_a_root_cause_and_hurts_nobody(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        set_status(self.devs['sw1'], 'down')
        self.assertEqual([o['device'].name for o in analyze_outages()[0]], ['sw1'])
        self.suppress('sw1', reason='backup, switched off')
        self.assertEqual(down_device_ids(), set())
        outages, explained = analyze_outages()
        self.assertEqual((outages, explained), ([], []))

    def test_dependents_that_are_really_down_become_root_causes_unless_included(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        for name in ('sw1', 'ap'):
            set_status(self.devs[name], 'down')
        suppression = self.suppress('sw1')
        self.assertEqual([o['device'].name for o in analyze_outages()[0]], ['ap'])        # ap is down by itself now
        suppression.include_dependents = True
        suppression.save()
        self.assertEqual(analyze_outages(), ([], []))                                     # everything below is ignored too
        info = suppressed_info()
        self.assertEqual((info[self.devs['sw1'].pk]['direct'], info[self.devs['ap'].pk]['direct'], info[self.devs['ap'].pk]['via']),
                         (True, False, 'sw1'))

    def test_a_suppression_ends_at_its_time(self):
        set_status(self.devs['core'], 'down')
        self.dep('core', 'sw1')
        self.suppress('core', until=timezone.now() + timedelta(hours=1))
        self.assertEqual(down_device_ids(), set())
        DeviceSuppression.objects.update(until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(down_device_ids(), {self.devs['core'].pk})
        self.assertEqual(suppressed_info(), {})


class SuppressionNotificationTests(NotifBase):
    def test_no_notification_for_a_suppressed_device(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        DeviceSuppression.objects.create(device=self.devs['core'], reason='planned')
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})
        self.assertEqual((self.sent, OutageNotification.objects.count()), ([], 0))

    def test_an_announced_outage_is_closed_quietly_when_the_device_is_suppressed(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.assertEqual(self.run_n()['sent'], 1)                           # announced
        DeviceSuppression.objects.create(device=self.devs['core'], reason='expected')
        self.sent.clear()
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})
        episode = OutageNotification.objects.get()
        self.assertEqual((episode.quiet, episode.resolved_at is not None), (True, True))
        self.assertEqual(self.sent, [])                                     # no "outage ended" message
        self.status('core', 'up')
        self.assertEqual(self.run_n()['resolved'], 0)

    def test_when_the_suppression_ends_a_real_outage_is_announced(self):
        self.dep('core', 'sw1')
        self.status('core', 'down', minutes_ago=30)
        DeviceSuppression.objects.create(device=self.devs['core'], until=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.run_n()['sent'], 0)
        DeviceSuppression.objects.update(until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.run_n()['sent'], 1)


class SuppressionPagesTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('su', 'su@example.com', 'x'))
        self.dep('core', 'sw1')

    def test_add_prefilled_with_a_return_url_and_edit(self):
        import re
        page = self.client.get(f'/plugins/dependencies/suppressions/add/?device={self.devs["core"].pk}'
                               '&return_url=/plugins/dependencies/graph/%3Fsite%3D1').content.decode()
        self.assertTrue(re.search(rf'<option value="{self.devs["core"].pk}"\s+selected', page))
        self.assertIn('name="return_url"', page)
        until = (timezone.now() + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        r = self.client.post('/plugins/dependencies/suppressions/add/', {
            'device': self.devs['core'].pk, 'reason': 'backup unit', 'until': until, 'include_dependents': 'on',
            'return_url': '/plugins/dependencies/graph/?site=1'})
        self.assertEqual((r.status_code, r['Location']), (302, '/plugins/dependencies/graph/?site=1'))
        row = DeviceSuppression.objects.get()
        self.assertEqual((row.reason, row.include_dependents, row.created_by.username), ('backup unit', True, 'su'))
        r = self.client.post(f'/plugins/dependencies/suppressions/{row.pk}/edit/', {
            'device': self.devs['core'].pk, 'reason': 'changed', 'until': '', 'include_dependents': ''})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.reason, row.until, row.include_dependents), ('changed', None, False))

    def test_validation_and_foreign_return_urls(self):
        past = (timezone.now() - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        r = self.client.post('/plugins/dependencies/suppressions/add/', {'device': self.devs['core'].pk, 'until': past})
        self.assertEqual((r.status_code, DeviceSuppression.objects.count()), (200, 0))      # the end must be in the future
        r = self.client.post('/plugins/dependencies/suppressions/add/', {
            'device': self.devs['core'].pk, 'return_url': 'https://evil.example/x'})
        self.assertEqual(r['Location'], '/plugins/dependencies/suppressions/')              # a foreign address is ignored

    def test_resume_by_json_and_by_form_and_permissions(self):
        row = DeviceSuppression.objects.create(device=self.devs['core'], reason='x')
        r = self.client.post(f'/plugins/dependencies/suppressions/{row.pk}/resume/', HTTP_ACCEPT='application/json')
        self.assertEqual((r.status_code, r.json()), (200, {'resumed': 'core'}))
        self.assertFalse(DeviceSuppression.objects.exists())
        row = DeviceSuppression.objects.create(device=self.devs['sw1'])
        r = self.client.post(f'/plugins/dependencies/suppressions/{row.pk}/resume/')
        self.assertEqual(r.status_code, 302)
        nobody = Client()
        nobody.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        self.assertEqual(nobody.get('/plugins/dependencies/suppressions/').status_code, 403)
        self.assertEqual(nobody.post('/plugins/dependencies/suppressions/1/resume/').status_code, 403)

    def test_list_graph_states_tree_and_live_page_show_it(self):
        set_status(self.devs['sw1'], 'down')
        DeviceSuppression.objects.create(device=self.devs['sw1'], reason='backup unit')
        listing = self.client.get('/plugins/dependencies/suppressions/').content.decode()
        self.assertIn('backup unit', listing)
        self.assertIn('down', listing)
        states = self.client.get('/api/plugins/dependencies/status/states/').json()
        self.assertEqual(states['suppressed'][str(self.devs['sw1'].pk)]['reason'], 'backup unit')
        self.assertEqual(states['roots'], [])                                    # not a root cause
        outages = self.client.get('/api/plugins/dependencies/outages/').json()
        self.assertEqual((outages['root_causes_total'], outages['suppressed'][0]['device']), (0, self.devs['sw1'].pk))
        graph = build_graph()
        node = next(x['data'] for x in graph['nodes'] if x['data']['devid'] == self.devs['sw1'].pk)
        self.assertEqual(node['suppressed']['reason'], 'backup unit')
        page = self.client.get('/plugins/dependencies/graph/').content.decode()
        self.assertIn('"suppress_url": "/plugins/dependencies/suppressions/add/"', page)
        tree = self.client.get('/plugins/dependencies/tree/?expanded=1').content.decode()
        self.assertIn('dep-chip-suppressed', tree)
        self.assertNotIn('class="dep-chip dep-chip-down"', tree)                # not shown as down
        live = self.client.get('/plugins/dependencies/live/').content.decode()
        self.assertIn('Suppressed devices', live)
        self.assertIn('down (ignored)', live)
