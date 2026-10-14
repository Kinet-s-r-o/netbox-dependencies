"""ICMP poller: settings from the UI, targets, heartbeat, the page and the "poller is silent" alerts."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from extras.models import Tag
from ipam.models import IPAddress
from netbox_dependencies import channels
from netbox_dependencies import notifications as n
from dcim.models import Device, Site
from netbox_dependencies.models import Dependency, DeviceStatus, Poller, PollerHeartbeat, PollerSettings
from netbox_dependencies.status import apply_icmp_report, poll_targets, poller_config, poller_settings
from netbox_dependencies.tests.base import Base
from netbox_dependencies.tests.test_notifications import NotifBase, card_text, slack_text

TARGETS = '/api/plugins/dependencies/status/targets/'
REPORT = '/api/plugins/dependencies/status/report/'


def give_ip(device, address):
    ip = IPAddress.objects.create(address=address)
    device.primary_ip4 = ip
    device.save()


class PollerBase(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('p', 'p@example.com', 'x'))
        give_ip(self.devs['core'], '10.0.0.1/24')
        give_ip(self.devs['sw1'], '10.0.0.2/24')
        give_ip(self.devs['x'], '10.0.0.9/24')       # a standalone device (no dependency)
        self.dep('core', 'sw1')

    def post(self, body):
        import json
        return self.client.post(REPORT, data=json.dumps(body), content_type='application/json')


class SettingsTests(PollerBase):
    def test_defaults_come_from_plugin_config_until_something_is_saved(self):
        cfg = poller_config()
        self.assertEqual((cfg['interval_seconds'], cfg['ping_count'], cfg['fail_threshold'], cfg['stale_after_seconds']),
                         (60, 3, 3, 600))
        PollerSettings.objects.create(interval_seconds=30, fail_threshold=5)
        cfg = poller_config()
        self.assertEqual((cfg['interval_seconds'], cfg['fail_threshold']), (30, 5))

    def test_failure_threshold_from_the_ui_is_used(self):
        PollerSettings.objects.create(fail_threshold=2)
        apply_icmp_report(self.devs['sw1'].pk, False)
        self.assertNotEqual(DeviceStatus.objects.get(device=self.devs['sw1']).state, 'down')
        apply_icmp_report(self.devs['sw1'].pk, False)
        self.assertEqual(DeviceStatus.objects.get(device=self.devs['sw1']).state, 'down')


class TargetsTests(PollerBase):
    def names(self, response):
        return sorted(t['name'] for t in response.json()['targets'])

    def test_only_devices_with_a_dependency_by_default_and_settings_are_returned(self):
        body = self.client.get(TARGETS).json()
        self.assertEqual(sorted(t['name'] for t in body['targets']), ['core', 'sw1'])
        self.assertEqual(body['settings'], {'enabled': True, 'interval_seconds': 60, 'ping_count': 3,
                                            'ping_timeout': 1.0, 'concurrency': 500})

    def test_setting_and_query_parameter_control_standalone_devices(self):
        PollerSettings.objects.create(only_in_graph=False)
        self.assertEqual(self.names(self.client.get(TARGETS)), ['core', 'sw1', 'x'])
        self.assertEqual(self.names(self.client.get(TARGETS + '?in_graph=1')), ['core', 'sw1'])

    def test_exclusion_tag(self):
        tag = Tag.objects.create(name='No ping', slug='no-ping')
        self.devs['sw1'].tags.add(tag)
        self.assertEqual(self.names(self.client.get(TARGETS)), ['core'])
        PollerSettings.objects.create(exclude_tag='')            # the exclusion is switched off
        self.assertEqual(self.names(self.client.get(TARGETS)), ['core', 'sw1'])

    def test_paused_poller_gets_no_targets_but_the_settings(self):
        PollerSettings.objects.create(enabled=False, interval_seconds=120)
        body = self.client.get(TARGETS).json()
        self.assertEqual((body['count'], body['targets']), (0, []))
        self.assertEqual((body['settings']['enabled'], body['settings']['interval_seconds']), (False, 120))

    def test_poll_targets_helper(self):
        self.assertEqual(sorted(d.name for d, _ip in poll_targets()), ['core', 'sw1'])


class HeartbeatTests(PollerBase):
    def test_heartbeat_with_results_and_without(self):
        r = self.post({'results': [{'device': self.devs['core'].pk, 'reachable': True}],
                       'poller': {'name': 'poller-1', 'cycle_seconds': 4.25, 'targets': 2, 'reachable': 1,
                                  'unreachable': 1, 'paused': False, 'version': '1.4.0'}})
        self.assertEqual((r.status_code, r.json()['updated']), (200, 1))
        beat = PollerHeartbeat.objects.get()
        self.assertEqual((beat.name, beat.cycle_seconds, beat.targets, beat.unreachable, beat.version),
                         ('poller-1', 4.25, 2, 1, '1.4.0'))
        first_seen = beat.first_seen
        r = self.post({'poller': {'name': 'poller-1', 'paused': True}})      # "results" may be left out with a heartbeat
        self.assertEqual(r.status_code, 200)
        beat.refresh_from_db()
        self.assertEqual((beat.paused, beat.first_seen), (True, first_seen))
        self.assertEqual(PollerHeartbeat.objects.count(), 1)

    def test_garbage_in_heartbeat_does_not_break_the_report(self):
        r = self.post({'results': [], 'poller': {'name': 'p', 'cycle_seconds': 'abc', 'targets': 'many'}})
        self.assertEqual(r.status_code, 200)
        beat = PollerHeartbeat.objects.get()
        self.assertEqual((beat.cycle_seconds, beat.targets), (None, 0))

    def test_results_are_still_required_without_a_heartbeat(self):
        self.assertEqual(self.post({}).status_code, 400)


class PollerPageTests(PollerBase):
    def valid(self, **over):
        data = {'enabled': 'on', 'interval_seconds': '45', 'ping_count': '2', 'ping_timeout': '0.8', 'concurrency': '200',
                'only_in_graph': 'on', 'fail_threshold': '4', 'stale_after_seconds': '300', 'exclude_tag': 'no-ping',
                'alert_enabled': 'on', 'alert_after_seconds': '240'}
        data.update(over)
        return data

    def test_page_shows_pollers_and_counts(self):
        PollerHeartbeat.objects.create(name='poller-1', first_seen=timezone.now(), last_seen=timezone.now(),
                                       cycle_seconds=3.2, targets=5, reachable=4, unreachable=1)
        page = self.client.get('/plugins/dependencies/poller/').content.decode()
        self.assertIn('poller-1', page)
        self.assertIn('reporting', page)
        self.assertIn('Which devices', page)

    def test_silent_poller_is_marked_on_the_page(self):
        PollerHeartbeat.objects.create(name='old', first_seen=timezone.now(), last_seen=timezone.now() - timedelta(hours=1))
        self.assertIn('silent', self.client.get('/plugins/dependencies/poller/').content.decode())

    def test_save_validates_and_persists(self):
        r = self.client.post('/plugins/dependencies/poller/', self.valid())
        self.assertEqual(r.status_code, 302)
        row = PollerSettings.objects.get()
        self.assertEqual((row.interval_seconds, row.ping_count, row.ping_timeout, row.fail_threshold, row.alert_after_seconds),
                         (45, 2, 0.8, 4, 240))
        bad = self.client.post('/plugins/dependencies/poller/', self.valid(stale_after_seconds='30', interval_seconds='60'))
        self.assertEqual(bad.status_code, 200)             # statuses must outlive one round
        self.assertEqual(PollerSettings.objects.get().interval_seconds, 45)

    def test_forget_a_poller_and_permissions(self):
        beat = PollerHeartbeat.objects.create(name='gone', first_seen=timezone.now(), last_seen=timezone.now())
        self.client.post('/plugins/dependencies/poller/', {'forget': beat.pk})
        self.assertFalse(PollerHeartbeat.objects.exists())
        viewer = Client()
        viewer.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        self.assertEqual(viewer.get('/plugins/dependencies/poller/').status_code, 403)


class RegisteredPollerTests(PollerBase):
    """Registered pollers: own settings, switching off, and scopes (sites)."""

    def setUp(self):
        super().setUp()
        self.branch = Site.objects.create(name='Branch', slug='branch')
        d = self.devs['sw1']
        remote = Device.objects.create(name='br-sw', site=self.branch, device_type=d.device_type, role=d.role)
        give_ip(remote, '10.9.0.1/24')
        Dependency.objects.create(upstream=self.devs['core'], downstream=remote)

    def names(self, name=None):
        url = TARGETS + (f'?poller={name}' if name else '')
        return sorted(t['name'] for t in self.client.get(url).json()['targets'])

    def test_unregistered_poller_uses_the_shared_settings_and_all_devices(self):
        self.assertEqual(self.names('whoever'), ['br-sw', 'core', 'sw1'])
        self.assertEqual(self.names(), ['br-sw', 'core', 'sw1'])

    def test_own_settings_override_only_what_is_filled_in(self):
        Poller.objects.create(name='branch', interval_seconds=15, ping_timeout=2.5)
        settings, entry = poller_settings('branch')
        self.assertEqual((settings['interval_seconds'], settings['ping_timeout'], settings['ping_count']), (15, 2.5, 3))
        self.assertEqual(entry.name, 'branch')
        body = self.client.get(TARGETS + '?poller=branch').json()['settings']
        self.assertEqual((body['interval_seconds'], body['ping_timeout'], body['concurrency']), (15, 2.5, 500))

    def test_disabled_poller_gets_nothing_but_others_are_unaffected(self):
        Poller.objects.create(name='off', enabled=False)
        body = self.client.get(TARGETS + '?poller=off').json()
        self.assertEqual((body['count'], body['settings']['enabled']), (0, False))
        self.assertEqual(self.names('other'), ['br-sw', 'core', 'sw1'])

    def test_sites_scope_splits_the_devices_between_pollers(self):
        mine = Poller.objects.create(name='branch')
        mine.sites.set([self.branch])
        self.assertEqual(self.names('branch'), ['br-sw'])             # only its sites
        self.assertEqual(self.names('main'), ['core', 'sw1'])         # the rest: everything not claimed
        self.assertEqual(self.names(), ['br-sw', 'core', 'sw1'])      # no name = no scope
        mine.enabled = False
        mine.save()
        self.assertEqual(self.names('main'), ['br-sw', 'core', 'sw1'])  # a disabled poller claims nothing

    def test_add_edit_and_unregister_through_the_pages(self):
        r = self.client.post('/plugins/dependencies/poller/add/', {
            'name': 'branch', 'description': 'Branch office', 'enabled': 'on', 'sites': [self.branch.pk],
            'interval_seconds': '30', 'ping_timeout': '1.5'})
        self.assertEqual(r.status_code, 302, getattr(r, 'content', b'')[:300])
        entry = Poller.objects.get()
        self.assertEqual((entry.description, entry.interval_seconds, entry.ping_timeout, entry.ping_count),
                         ('Branch office', 30, 1.5, None))
        self.assertEqual(list(entry.sites.all()), [self.branch])
        self.assertEqual(self.client.get(f'/plugins/dependencies/poller/{entry.pk}/edit/').status_code, 200)
        r = self.client.post(f'/plugins/dependencies/poller/{entry.pk}/edit/', {
            'name': 'branch', 'enabled': 'on', 'interval_seconds': '', 'sites': []})
        self.assertEqual(r.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual((entry.interval_seconds, entry.sites.count(), entry.description), (None, 0, ''))
        dup = self.client.post('/plugins/dependencies/poller/add/', {'name': 'BRANCH', 'enabled': 'on'})
        self.assertEqual(dup.status_code, 200)                         # the name is unique (case-insensitive)
        self.assertEqual(Poller.objects.count(), 1)
        self.client.post(f'/plugins/dependencies/poller/{entry.pk}/delete/')
        self.assertFalse(Poller.objects.exists())

    def test_page_lists_registered_and_auto_detected_pollers(self):
        Poller.objects.create(name='planned', description='Not started yet')
        PollerHeartbeat.objects.create(name='stranger', first_seen=timezone.now(), last_seen=timezone.now())
        page = self.client.get('/plugins/dependencies/poller/').content.decode()
        self.assertIn('never reported', page)       # registered, no heartbeat yet
        self.assertIn('auto-detected', page)        # heartbeat without a registration
        self.assertIn('Register', page)

    def test_only_those_who_may_change_can_add(self):
        viewer = Client()
        viewer.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        self.assertEqual(viewer.get('/plugins/dependencies/poller/add/').status_code, 403)


class AlertTests(NotifBase):
    """A poller that goes silent raises one alert, and one message when it is back."""

    def setUp(self):
        super().setUp()
        self.slack = n.NotificationDestination.objects.create(name='Slack', kind='slack', webhook_url=self.SLACK_URL)
        self.now = timezone.now()
        self.beat = PollerHeartbeat.objects.create(name='poller-1', first_seen=self.now - timedelta(days=1),
                                                   last_seen=self.now)

    def run_alerts(self, at=None):
        return n.run_poller_alerts(at or self.now)

    def test_silent_poller_alerts_once_and_only_after_the_limit(self):
        self.assertEqual(self.run_alerts(self.now + timedelta(minutes=4)), {'down': 0, 'up': 0, 'errors': 0})
        result = self.run_alerts(self.now + timedelta(minutes=6))
        self.assertEqual((result['down'], len(self.sent)), (1, 2))          # one alert to each destination
        self.assertIn('not reporting', card_text(self.to(self.TEAMS_URL)[0]['card']).lower())
        self.assertIn('poller-1', slack_text(self.to(self.SLACK_URL)[0]))
        self.assertEqual(self.run_alerts(self.now + timedelta(minutes=30))['down'], 0)     # no repeat
        self.assertEqual(len(self.sent), 2)

    def test_back_message_after_it_reports_again(self):
        self.run_alerts(self.now + timedelta(minutes=6))
        self.sent.clear()
        PollerHeartbeat.objects.filter(pk=self.beat.pk).update(last_seen=self.now + timedelta(minutes=9))
        result = self.run_alerts(self.now + timedelta(minutes=9))
        self.assertEqual((result['up'], len(self.sent)), (1, 2))
        self.assertIn('back', card_text(self.to(self.TEAMS_URL)[0]['card']).lower())
        self.assertIsNone(PollerHeartbeat.objects.get().alerted_at)
        self.assertEqual(self.run_alerts(self.now + timedelta(minutes=10))['up'], 0)

    def test_no_back_message_when_recovery_messages_are_off(self):
        self.run_alerts(self.now + timedelta(minutes=6))
        self.sent.clear()
        self.cfg['notify_recovery'] = False
        PollerHeartbeat.objects.filter(pk=self.beat.pk).update(last_seen=self.now + timedelta(minutes=9))
        self.run_alerts(self.now + timedelta(minutes=9))
        self.assertEqual(self.sent, [])
        self.assertIsNone(PollerHeartbeat.objects.get().alerted_at)

    def test_failed_delivery_is_retried_next_minute(self):
        self.fail_urls.update({self.TEAMS_URL, self.SLACK_URL})
        result = self.run_alerts(self.now + timedelta(minutes=6))
        self.assertEqual((result['down'], result['errors']), (0, 2))
        self.assertIsNone(PollerHeartbeat.objects.get().alerted_at)
        self.fail_urls.clear()
        self.assertEqual(self.run_alerts(self.now + timedelta(minutes=7))['down'], 1)

    def test_switched_off_alerts_and_missing_destinations(self):
        PollerSettings.objects.create(alert_enabled=False)
        self.assertIn('skipped', self.run_alerts(self.now + timedelta(hours=1)))
        PollerSettings.objects.update(alert_enabled=True)
        n.NotificationDestination.objects.update(enabled=False)
        self.assertIn('skipped', self.run_alerts(self.now + timedelta(hours=1)))
        self.assertEqual(self.sent, [])

    def test_switched_off_poller_does_not_alert(self):
        Poller.objects.create(name='poller-1', enabled=False)
        self.assertEqual(self.run_alerts(self.now + timedelta(hours=2)), {'down': 0, 'up': 0, 'errors': 0})
        self.assertEqual(self.sent, [])

    def test_message_formats(self):
        view = {'name': 'poller-1', 'last_seen': self.now, 'silent_seconds': 900, 'url': 'https://netbox.example.com/p/'}
        generic = channels.render_poller('generic', 'down', view, self.now)
        self.assertEqual((generic['event'], generic['poller']['name'], generic['poller']['silent_seconds']),
                         ('poller_down', 'poller-1', 900))
        teams = channels.render_poller('teams', 'up', dict(view, url=''), self.now)
        self.assertNotIn('actions', teams['card'])                         # no public URL = no button
        with_button = channels.render_poller('teams', 'down', view, self.now)['card']
        self.assertEqual(with_button['actions'][0]['url'], view['url'])
