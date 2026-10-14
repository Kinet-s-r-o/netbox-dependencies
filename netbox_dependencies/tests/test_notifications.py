"""Notifications (Teams / Slack / generic webhook), destinations, settings and message formats."""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from netbox_dependencies import channels
from netbox_dependencies import notifications as n
from netbox_dependencies.models import (
    DeviceStatus,
    NotificationDelivery,
    NotificationDestination,
    NotificationSettings,
    OutageNotification,
)
from netbox_dependencies.notifications import DeliveryError
from netbox_dependencies.tests.base import Base

REAL_POST = n._post   # the real function (NotifBase replaces it with a fake in the tests)


def walk(node):
    """All nested dict items (Adaptive Card / Block Kit)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def card_text(card):
    return ' | '.join(item['text'] for item in walk(card) if item.get('type') == 'TextBlock')


def slack_text(payload):
    parts = []
    for item in walk(payload):
        text = item.get('text')
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(text, dict) and 'text' in text:
            parts.append(text['text'])
    return ' | '.join(parts)


class NotifBase(Base):
    """Settings are faked (no env / DB row) and sending is captured — nothing is sent out."""

    CFG = {'enabled': True, 'delay_seconds': 120, 'notify_recovery': True, 'public_url': 'https://netbox.example.com',
           'status_stale_after': 600, 'fail_threshold': 3}
    TEAMS_URL = 'https://teams.example/hook'
    SLACK_URL = 'https://hooks.slack.example/services/T/B/X'

    def setUp(self):
        self.cfg = dict(self.CFG)
        for target in ('netbox_dependencies.notifications._setting', 'netbox_dependencies.status._setting'):
            patcher = mock.patch(target, side_effect=lambda key: self.cfg[key])
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sent = []          # (url, payload, secret)
        self.fail_urls = set()  # addresses that will reject the request

        def fake_post(url, payload, secret=''):
            if url in self.fail_urls:
                raise DeliveryError('HTTP 500')
            self.sent.append((url, payload, secret))

        patcher = mock.patch('netbox_dependencies.notifications._post', side_effect=fake_post)
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.teams = NotificationDestination.objects.create(name='Teams', kind='teams', webhook_url=self.TEAMS_URL)

    def status(self, name, state, minutes_ago=10, source='icmp', detail='no ICMP reply'):
        now = timezone.now()
        DeviceStatus.objects.update_or_create(
            device=self.devs[name],
            defaults=dict(state=state, source=source, detail=detail, last_checked=now,
                          last_changed=now - timedelta(minutes=minutes_ago)))

    def run_n(self):
        return n.run_notifications()

    def to(self, url):
        return [payload for sent_url, payload, _secret in self.sent if sent_url == url]


# ====================================================================================== logika (Teams)
class OutageLogicTests(NotifBase):
    def test_nothing_without_enabled_destination_or_when_switched_off(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.cfg['enabled'] = False
        self.assertIn('skipped', self.run_n())
        self.cfg['enabled'] = True
        NotificationDestination.objects.update(enabled=False)
        self.assertIn('skipped', self.run_n())
        self.assertEqual((self.sent, OutageNotification.objects.count()), ([], 0))

    def test_root_cause_is_announced_once_with_card_and_children_are_not(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        self.status('core', 'down')
        self.status('sw1', 'down')          # explained by the outage of core -> no notification of its own
        self.assertEqual(self.run_n(), {'sent': 1, 'resolved': 0, 'errors': 0})
        url, payload, _ = self.sent[0]
        self.assertEqual(url, self.TEAMS_URL)
        card = payload['card']
        self.assertEqual((card['type'], card['version']), ('AdaptiveCard', '1.4'))
        self.assertIn('core', payload['text'])
        self.assertNotIn('sw1 (', payload['text'])
        self.assertEqual(json.loads(payload['card_json']), card)
        self.assertIn('sw1', json.dumps(card, ensure_ascii=False))        # sw1 is among the affected devices
        urls = [a['url'] for a in walk(card) if a.get('type') == 'Action.OpenUrl']
        self.assertTrue(urls[0].startswith('https://netbox.example.com/plugins/dependencies/graph/?site='))
        self.assertIn(f'focus={self.devs["core"].pk}', urls[0])
        self.assertIn('impact=1', urls[0])      # the graph opens with the outage already simulated
        self.assertTrue(any(u.endswith(f'/dcim/devices/{self.devs["core"].pk}/') for u in urls))
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})     # no duplicate
        self.assertEqual(len(self.sent), 1)
        episode = OutageNotification.objects.get()
        self.assertEqual((episode.device_id, episode.peak_down), (self.devs['core'].pk, 2))
        self.assertIsNotNone(NotificationDelivery.objects.get(kind='outage').sent_at)

    def test_device_without_any_dependency_is_not_announced(self):
        self.dep('core', 'sw1')
        self.status('sw2', 'down')          # monitored and down, but not part of any dependency
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})
        self.assertEqual((self.sent, OutageNotification.objects.count()), ([], 0))

    def test_short_outage_waits_for_the_delay(self):
        self.dep('core', 'sw1')
        self.status('core', 'down', minutes_ago=1)
        self.run_n()
        self.assertEqual((self.sent, OutageNotification.objects.count()), ([], 0))
        self.cfg['delay_seconds'] = 30           # the delay is configurable
        self.assertEqual(self.run_n()['sent'], 1)

    def test_recovery_message_after_device_is_up_again(self):
        self.dep('core', 'sw1')
        self.status('core', 'down', minutes_ago=30)
        self.run_n()
        self.status('core', 'up', minutes_ago=0)
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 1, 'errors': 0})
        _url, payload, _ = self.sent[-1]
        self.assertIn('ended', payload['text'])
        self.assertIn('core', payload['text'])
        self.assertIsNotNone(OutageNotification.objects.get().resolved_at)
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})     # the recovery is sent once
        self.assertEqual(len(self.sent), 2)
        self.status('core', 'down', minutes_ago=5)                                  # a new outage = a new notification
        self.assertEqual(self.run_n()['sent'], 1)
        self.assertEqual(OutageNotification.objects.count(), 2)

    def test_several_new_root_causes_are_grouped_into_one_message(self):
        self.dep('core', 'sw1')
        self.dep('x', 'sw2')                # sw2 is part of the graph, its upstream is fine
        self.status('core', 'down')
        self.status('sw2', 'down')
        self.assertEqual(self.run_n()['sent'], 2)
        self.assertEqual(len(self.sent), 1)
        self.assertIn('2 root causes', self.sent[0][1]['text'])

    def test_recovery_can_be_switched_off(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.run_n()
        self.cfg['notify_recovery'] = False
        self.status('core', 'up', minutes_ago=0)
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(NotificationDelivery.objects.filter(kind='recovery').exists())


class RetryTests(NotifBase):
    def test_failed_send_is_retried_and_does_not_lose_the_outage(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.fail_urls.add(self.TEAMS_URL)
        self.assertEqual(self.run_n()['errors'], 1)
        delivery = NotificationDelivery.objects.get()
        self.assertEqual((delivery.sent_at, delivery.attempts, delivery.last_error), (None, 1, 'HTTP 500'))
        self.teams.refresh_from_db()
        self.assertEqual((self.teams.last_ok, self.teams.last_error), (False, 'HTTP 500'))   # the result is visible in the UI
        self.fail_urls.clear()
        self.assertEqual(self.run_n()['sent'], 1)
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.sent_at)
        self.assertEqual(delivery.last_error, '')

    def test_outage_that_was_never_announced_has_no_recovery_message(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.fail_urls.add(self.TEAMS_URL)
        self.run_n()
        self.status('core', 'up', minutes_ago=0)
        self.fail_urls.clear()
        self.assertEqual(self.run_n(), {'sent': 0, 'resolved': 0, 'errors': 0})
        self.assertEqual(self.sent, [])

    def test_retry_stops_after_max_attempts(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.fail_urls.add(self.TEAMS_URL)
        for _ in range(n.MAX_ATTEMPTS + 3):
            self.run_n()
        self.assertEqual(NotificationDelivery.objects.get().attempts, n.MAX_ATTEMPTS)


# ====================================================================================== multiple destinations
class MultiDestinationTests(NotifBase):
    def setUp(self):
        super().setUp()
        self.slack = NotificationDestination.objects.create(name='Slack', kind='slack', webhook_url=self.SLACK_URL)
        self.generic = NotificationDestination.objects.create(
            name='Hook', kind='generic', webhook_url='https://hook.example/in', secret='s3cret')

    def test_each_destination_gets_its_own_format(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.assertEqual(self.run_n()['sent'], 3)
        teams, = self.to(self.TEAMS_URL)
        slack, = self.to(self.SLACK_URL)
        generic, = self.to('https://hook.example/in')
        self.assertIn('card', teams)
        self.assertNotIn('attachments', teams)
        self.assertIn('attachments', slack)
        self.assertNotIn('card', slack)
        self.assertEqual((generic['event'], generic['version'], generic['source']), ('outage', 1, 'netbox-dependencies'))
        self.assertEqual(generic['outages'][0]['device']['name'], 'core')
        self.assertEqual(generic['outages'][0]['impact'], {'down': 1, 'degraded': 0, 'affected': ['sw1']})
        # only the generic webhook gets the signing key
        secrets = {url: secret for url, _payload, secret in self.sent}
        self.assertEqual(secrets, {self.TEAMS_URL: '', self.SLACK_URL: '', 'https://hook.example/in': 's3cret'})

    def test_only_the_failing_destination_is_retried(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.fail_urls.add(self.SLACK_URL)
        result = self.run_n()
        self.assertEqual((result['sent'], result['errors']), (2, 1))
        self.fail_urls.clear()
        result = self.run_n()
        self.assertEqual((result['sent'], result['errors']), (1, 0))
        self.assertEqual(len(self.to(self.TEAMS_URL)), 1)               # Teams is not repeated
        self.assertEqual(len(self.to(self.SLACK_URL)), 1)               # Slack got the message only now
        self.assertEqual(len(self.to('https://hook.example/in')), 1)

    def test_recovery_goes_only_where_the_outage_was_announced(self):
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.fail_urls.add(self.SLACK_URL)               # Slack never received the outage message
        self.run_n()
        self.status('core', 'up', minutes_ago=0)
        self.fail_urls.clear()
        self.assertEqual(self.run_n()['resolved'], 2)    # Teams + generic, not Slack
        self.assertEqual(len(self.to(self.SLACK_URL)), 0)
        self.assertIn('ended', self.to(self.TEAMS_URL)[-1]['text'])

    def test_disabled_destination_receives_nothing(self):
        self.slack.enabled = False
        self.slack.save()
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.run_n()
        self.assertEqual(self.to(self.SLACK_URL), [])
        self.assertEqual(len(self.sent), 2)

    def test_destination_added_during_an_outage_gets_the_ongoing_outage_and_later_recovery(self):
        self.slack.delete()
        self.generic.delete()
        self.dep('core', 'sw1')
        self.status('core', 'down')
        self.run_n()
        NotificationDestination.objects.create(name='Late', kind='slack', webhook_url='https://late.example/x')
        self.assertEqual(self.run_n()['sent'], 1)
        self.assertEqual(len(self.to('https://late.example/x')), 1)
        self.status('core', 'up', minutes_ago=0)
        self.assertEqual(self.run_n()['resolved'], 2)


# ====================================================================================== message formats
class RendererTests(NotifBase):
    def views(self, count=2, names=None, now=None):
        now = now or timezone.now()
        return [{'device_id': i + 1, 'name': (names or {}).get(i, f'dev-{i:02d}'), 'site': 'Site', 'since': now - timedelta(minutes=30),
                 'down': 5 - min(i, 5), 'degraded': 1 if i == 0 else 0, 'names': [f'child-{i}-{j}' for j in range(3)],
                 'source': 'ICMP poller — no ICMP reply', 'affected_pks': {i * 10 + j for j in range(4)},
                 'graph_url': f'http://nb/graph/{i}', 'device_url': f'http://nb/dev/{i}'} for i in range(count)]

    def test_teams_card_layout(self):
        self.dep('core', 'sw1')
        self.dep('core', 'sw2')
        self.dep('x', 'ap')                 # ap is a root cause with no downstream devices
        self.status('core', 'down', minutes_ago=75)
        self.status('sw1', 'down')
        self.status('sw2', 'down')
        self.status('ap', 'down')
        self.run_n()
        card = self.sent[0][1]['card']
        top = card['body']
        self.assertEqual((top[0]['type'], top[0]['style']), ('Container', 'attention'))
        header = card_text(top[0])
        self.assertIn('2 root causes', header)
        self.assertIn('4 devices down in total', header)
        tiles = [b for b in top if b['type'] == 'Container' and b.get('style') == 'emphasis']
        self.assertEqual(len(tiles), 2)
        self.assertIn('lasting 1 h 15 min', card_text(tiles[0]))
        self.assertTrue(any(i['type'] == 'ActionSet' for i in tiles[0]['items']))
        self.assertIn('No further impact', card_text(tiles[1]))
        self.assertNotIn('actions', card)
        self.assertNotIn('0 down', card_text(card))

    def test_slack_message_structure_and_escaping(self):
        now = timezone.now()
        views = self.views(2, names={0: 'AP <b>&co'}, now=now)
        payload = channels.render_outage('slack', views, now)
        json.dumps(payload)
        attachment, = payload['attachments']
        self.assertEqual(attachment['color'], '#d63939')
        types = [b['type'] for b in attachment['blocks']]
        self.assertEqual(types[0], 'header')
        self.assertIn('divider', types)
        self.assertEqual(types.count('actions'), 2)                       # buttons on every tile
        text = slack_text(payload)
        self.assertIn('AP &lt;b&gt;&amp;co', text)                        # mrkdwn is escaped
        self.assertNotIn('<b>', text)
        buttons = [e for b in attachment['blocks'] if b['type'] == 'actions' for e in b['elements']]
        self.assertTrue(all(e['url'].startswith('http://nb/') for e in buttons))
        self.assertLessEqual(len(attachment['blocks']), 50)
        recovery = channels.render_recovery('slack', [{**self.views(1)[0], 'until': now}], now)
        self.assertEqual(recovery['attachments'][0]['color'], '#2fb344')
        self.assertIn('Outage ended', slack_text(recovery))

    def test_slack_without_public_url_has_no_buttons(self):
        views = self.views(1)
        views[0]['graph_url'] = views[0]['device_url'] = ''
        payload = channels.render_outage('slack', views, timezone.now())
        self.assertNotIn('actions', [b['type'] for b in payload['attachments'][0]['blocks']])
        card = channels.render_outage('teams', views, timezone.now())['card']
        self.assertFalse([a for a in walk(card) if a.get('type') == 'Action.OpenUrl'])

    def test_generic_payload_is_plain_json_event(self):
        now = timezone.now()
        views = self.views(3, now=now)
        payload = channels.render_outage('generic', views, now)
        json.dumps(payload)
        self.assertEqual((payload['event'], payload['test'], payload['version']), ('outage', False, 1))
        self.assertEqual(len(payload['outages']), 3)
        first = payload['outages'][0]
        self.assertEqual(set(first), {'device', 'started_at', 'duration_seconds', 'source', 'impact'})
        self.assertEqual(first['duration_seconds'], 1800)
        self.assertEqual(first['device']['graph_url'], 'http://nb/graph/0')
        recovery = channels.render_recovery('generic', [{**views[0], 'until': now}], now)
        self.assertEqual(recovery['event'], 'recovery')
        self.assertEqual(recovery['recoveries'][0]['peak_impact'], {'down': 5, 'degraded': 1})

    def test_large_outages_are_summarised_in_every_format(self):
        now = timezone.now()
        views = self.views(channels.MAX_ROOTS + 3)
        teams = channels.render_outage('teams', views, now)
        self.assertIn('… and 3 more', card_text(teams['card']))
        self.assertEqual(sum(1 for b in teams['card']['body'] if b.get('style') == 'emphasis'), channels.MAX_ROOTS)
        slack = channels.render_outage('slack', views, now)
        self.assertIn('… and 3 more', slack_text(slack))
        self.assertLessEqual(len(slack['attachments'][0]['blocks']), 50)
        self.assertIn('… and 3 more', teams['text'])
        self.assertEqual(len(channels.render_outage('generic', views, now)['outages']), channels.MAX_ROOTS + 3)

    def test_samples_for_every_kind_are_marked_as_test(self):
        for kind in ('teams', 'slack', 'generic'):
            outage, recovery = n.sample_payloads(kind)
            for payload in (outage, recovery):
                json.dumps(payload)
                self.assertTrue(payload['text'].startswith('🧪 Sample'), kind)
        teams_outage, _ = n.sample_payloads('teams')
        slack_outage, _ = n.sample_payloads('slack')
        generic_outage, generic_recovery = n.sample_payloads('generic')
        self.assertIn('this is not a real outage', card_text(teams_outage['card']))
        self.assertIn('this is not a real outage', slack_text(slack_outage))
        self.assertTrue(generic_outage['test'] and generic_recovery['test'])

    def test_plural_helper_and_duration(self):
        self.assertEqual([channels.plural(k, 'a', 'b') for k in (0, 1, 2, 5)], ['b', 'a', 'b', 'b'])
        self.assertEqual((channels.duration(timedelta(minutes=5)), channels.duration(timedelta(minutes=135)),
                          channels.duration(timedelta(hours=50))), ('5 min', '2 h 15 min', '2 d 2 h'))


# ====================================================================================== sending (HTTP)
class PostTests(NotifBase):
    @staticmethod
    def _urlopen(captured, status=200):
        class Response:
            def __init__(self, code):
                self.status = code

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(request, timeout=0):
            captured['request'] = request
            return Response(status)

        return mock.patch('netbox_dependencies.notifications.urllib.request.urlopen', side_effect=fake)

    def test_the_body_is_signed_only_when_a_secret_is_given(self):
        captured = {}
        with self._urlopen(captured):
            REAL_POST('https://x.example/in', {'a': 'café'}, secret='k')
        request = captured['request']
        body = request.data
        self.assertEqual(request.get_header('X-signature-256'), 'sha256=' + hmac.new(b'k', body, hashlib.sha256).hexdigest())
        self.assertEqual(json.loads(body.decode('utf-8')), {'a': 'café'})
        self.assertEqual(request.get_header('Content-type'), 'application/json; charset=utf-8')
        captured.clear()
        with self._urlopen(captured):
            REAL_POST('https://x.example/in', {'a': 1})
        self.assertIsNone(captured['request'].get_header('X-signature-256'))

    def test_send_to_signs_only_generic_destinations(self):
        generic = NotificationDestination.objects.create(name='G', kind='generic', webhook_url='https://g.example/x', secret='s')
        slack = NotificationDestination.objects.create(name='S', kind='slack', webhook_url='https://s.example/x', secret='ignored')
        n.send_to(generic, {'a': 1})
        n.send_to(slack, {'a': 1})
        self.assertEqual([secret for _url, _payload, secret in self.sent], ['s', ''])

    def test_http_errors_become_delivery_errors_without_leaking_the_url(self):
        from urllib.error import HTTPError
        url = 'https://secret.example/hook?sig=SUPERSECRET'
        with mock.patch('netbox_dependencies.notifications.urllib.request.urlopen',
                        side_effect=HTTPError(url, 403, 'Forbidden', {}, None)), \
                mock.patch('netbox_dependencies.notifications.time.sleep'):
            with self.assertRaises(DeliveryError) as raised:
                REAL_POST(url, {})
        self.assertEqual(str(raised.exception), 'HTTP 403')

    def test_network_errors_are_reported_by_type_only(self):
        with mock.patch('netbox_dependencies.notifications.urllib.request.urlopen', side_effect=TimeoutError('slow')), \
                mock.patch('netbox_dependencies.notifications.time.sleep'):
            with self.assertRaises(DeliveryError) as raised:
                REAL_POST('https://x.example/in', {})
        self.assertEqual(str(raised.exception), 'TimeoutError')


# ====================================================================================== env and settings
class SettingsResolutionTests(Base):
    ENV = {'notifications_enabled': True, 'notification_delay_seconds': 120, 'notify_recovery': True,
           'public_url': 'http://env-host:8001', 'teams_webhook_url': 'https://env.example/hook'}

    def test_saved_values_override_environment_and_blank_public_url_falls_back(self):
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda plugin, key: self.ENV[key]):
            cfg = n.effective_settings()
            self.assertEqual((cfg['enabled'], cfg['delay_seconds'], cfg['public_url']), (True, 120, 'http://env-host:8001'))
            NotificationSettings.objects.create(enabled=False, delay_seconds=30, notify_recovery=False, public_url='')
            cfg = n.effective_settings()
            self.assertEqual((cfg['enabled'], cfg['delay_seconds'], cfg['notify_recovery'], cfg['public_url']),
                             (False, 30, False, 'http://env-host:8001'))
            NotificationSettings.objects.update(public_url='http://ui:8001')
            self.assertEqual(n.effective_settings()['public_url'], 'http://ui:8001')

    def test_webhook_from_environment_is_taken_over_once(self):
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda plugin, key: self.ENV[key]):
            destination = n.seed_from_environment()
            self.assertEqual((destination.name, destination.kind, destination.webhook_url),
                             ('Teams', 'teams', 'https://env.example/hook'))
            self.assertTrue(NotificationSettings.objects.exists())
            self.assertIsNone(n.seed_from_environment())                 # not created again
            destination.delete()
            self.assertIsNone(n.seed_from_environment())                 # not even after the destination is deleted

    def test_nothing_is_seeded_without_environment_url(self):
        env = dict(self.ENV, teams_webhook_url='')
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda plugin, key: env[key]):
            self.assertIsNone(n.seed_from_environment())
        self.assertFalse(NotificationDestination.objects.exists())


# ====================================================================================== UI: settings and destinations
class SettingsPageTests(Base):
    SECRET = 'https://prod-12.westeurope.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SuperSecretSig123'

    def _client(self, name, csrf=False, superuser=True):
        from django.contrib.auth import get_user_model
        from django.test import Client
        client = Client(enforce_csrf_checks=csrf)
        user_model = get_user_model()
        user = (user_model.objects.create_superuser(name, f'{name}@example.com', 'x') if superuser
                else user_model.objects.create_user(name, f'{name}@example.com', 'x'))
        client.force_login(user)
        return client

    def _token(self, client):
        import re
        page = client.get('/plugins/dependencies/settings/').content.decode()
        return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)

    def _add(self, client, token, **data):
        base = {'name': 'Teams', 'kind': 'teams', 'webhook_url': self.SECRET, 'enabled': 'on'}
        base.update(data)
        return client.post('/plugins/dependencies/settings/destinations/add/', base, HTTP_X_CSRFTOKEN=token)

    def test_general_settings_are_saved(self):
        client = self._client('adm', csrf=True)
        token = self._token(client)
        response = client.post('/plugins/dependencies/settings/', {
            'enabled': 'on', 'delay_seconds': '45', 'notify_recovery': '', 'public_url': 'https://netbox.example.com/'},
            HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 302)
        row = NotificationSettings.objects.get()
        self.assertEqual((row.enabled, row.delay_seconds, row.notify_recovery, row.public_url, row.updated_by.username),
                         (True, 45, False, 'https://netbox.example.com', 'adm'))
        bad = client.post('/plugins/dependencies/settings/', {'enabled': 'on', 'delay_seconds': '-5'}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(bad.status_code, 200)

    def test_destination_lifecycle_and_secrets_are_never_shown(self):
        client = self._client('adm', csrf=True)
        token = self._token(client)
        self.assertEqual(self._add(client, token).status_code, 302)
        destination = NotificationDestination.objects.get()
        self.assertEqual((destination.name, destination.kind, destination.webhook_url), ('Teams', 'teams', self.SECRET))
        page = client.get('/plugins/dependencies/settings/').content.decode()
        self.assertNotIn('SuperSecretSig123', page)                              # the URL is never printed
        self.assertIn('prod-12.westeurope.logic.azure.com', page)
        edit_url = f'/plugins/dependencies/settings/destinations/{destination.pk}/edit/'
        self.assertNotIn('SuperSecretSig123', client.get(edit_url).content.decode())
        # an edit with an empty URL keeps the saved one; changing the type to generic and a signing key
        client.post(edit_url, {'name': 'Hook', 'kind': 'generic', 'webhook_url': '', 'secret': 'k1', 'enabled': 'on'},
                    HTTP_X_CSRFTOKEN=token)
        destination.refresh_from_db()
        self.assertEqual((destination.name, destination.kind, destination.webhook_url, destination.secret),
                         ('Hook', 'generic', self.SECRET, 'k1'))
        page = client.get('/plugins/dependencies/settings/').content.decode()
        self.assertNotIn('>k1<', page)
        client.post(edit_url, {'name': 'Hook', 'kind': 'generic', 'clear_secret': 'on', 'enabled': 'on'}, HTTP_X_CSRFTOKEN=token)
        destination.refresh_from_db()
        self.assertEqual(destination.secret, '')
        # a type other than generic never carries a signing key
        client.post(edit_url, {'name': 'Hook', 'kind': 'generic', 'secret': 'k2', 'enabled': 'on'}, HTTP_X_CSRFTOKEN=token)
        client.post(edit_url, {'name': 'Slack', 'kind': 'slack', 'secret': 'k3', 'enabled': ''}, HTTP_X_CSRFTOKEN=token)
        destination.refresh_from_db()
        self.assertEqual((destination.kind, destination.secret, destination.enabled), ('slack', '', False))
        # deletion
        delete = client.post(f'/plugins/dependencies/settings/destinations/{destination.pk}/delete/', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(delete.status_code, 302)
        self.assertFalse(NotificationDestination.objects.exists())

    def test_destination_validation(self):
        client = self._client('adm', csrf=True)
        token = self._token(client)
        self.assertEqual(self._add(client, token, webhook_url='').status_code, 200)              # the URL is mandatory
        self.assertEqual(self._add(client, token, webhook_url='not a url').status_code, 200)
        self.assertEqual(self._add(client, token, name='').status_code, 200)
        self.assertEqual(self._add(client, token, kind='email').status_code, 200)
        self.assertFalse(NotificationDestination.objects.exists())
        self.assertEqual(self._add(client, token).status_code, 302)
        self.assertEqual(self._add(client, token, name='teams').status_code, 200)                # the name is unique (case-insensitive)

    def test_test_button_sends_samples_to_that_destination_only(self):
        client = self._client('adm')
        first = NotificationDestination.objects.create(name='A', kind='slack', webhook_url='https://a.example/x')
        second = NotificationDestination.objects.create(name='B', kind='teams', webhook_url='https://b.example/x')
        with mock.patch('netbox_dependencies.views.send_samples') as send:
            response = client.post(f'/plugins/dependencies/settings/destinations/{first.pk}/test/', follow=True)
            send.assert_called_once_with(first)
            self.assertIn('accepted by "A"', ' '.join(str(m) for m in response.context['messages']))
            send.side_effect = DeliveryError('HTTP 403')
            response = client.post(f'/plugins/dependencies/settings/destinations/{second.pk}/test/', follow=True)
            self.assertIn('HTTP 403', ' '.join(str(m) for m in response.context['messages']))

    def test_send_samples_posts_two_messages_and_records_the_result(self):
        destination = NotificationDestination.objects.create(name='S', kind='slack', webhook_url='https://s.example/x')
        sent = []
        with mock.patch('netbox_dependencies.notifications._post', side_effect=lambda url, payload, secret='': sent.append(payload)):
            n.send_samples(destination)
        self.assertEqual(len(sent), 2)
        self.assertTrue(all('attachments' in p for p in sent))
        destination.refresh_from_db()
        self.assertEqual((destination.last_ok, destination.last_error), (True, ''))
        self.assertIsNotNone(destination.last_attempt_at)
        with mock.patch('netbox_dependencies.notifications._post', side_effect=DeliveryError('HTTP 404')):
            with self.assertRaises(DeliveryError):
                n.send_samples(destination)
        destination.refresh_from_db()
        self.assertEqual((destination.last_ok, destination.last_error), (False, 'HTTP 404'))

    def test_permissions(self):
        client = self._client('nobody', superuser=False)
        destination = NotificationDestination.objects.create(name='A', kind='teams', webhook_url='https://a.example/x')
        for method, url in (
            ('get', '/plugins/dependencies/settings/'),
            ('get', '/plugins/dependencies/settings/destinations/add/'),
            ('post', f'/plugins/dependencies/settings/destinations/{destination.pk}/test/'),
            ('post', f'/plugins/dependencies/settings/destinations/{destination.pk}/delete/'),
        ):
            self.assertEqual(getattr(client, method)(url).status_code, 403, url)
        self.assertTrue(NotificationDestination.objects.exists())

    def test_command_sends_to_enabled_destinations_or_a_named_one(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('dependencies_test_notifications')                      # no destination
        NotificationDestination.objects.create(name='On', kind='teams', webhook_url='https://on.example/x')
        NotificationDestination.objects.create(name='Off', kind='slack', webhook_url='https://off.example/x', enabled=False)
        sent = []
        with mock.patch('netbox_dependencies.notifications._post', side_effect=lambda url, payload, secret='': sent.append(url)):
            call_command('dependencies_test_notifications', stdout=StringIO())
            self.assertEqual(sent, ['https://on.example/x'] * 2)
            sent.clear()
            call_command('dependencies_test_notifications', destination='off', stdout=StringIO())
            self.assertEqual(sent, ['https://off.example/x'] * 2)
            with self.assertRaises(CommandError):
                call_command('dependencies_test_notifications', destination='nope')

    def test_menu_and_page_render(self):
        client = self._client('menu')
        NotificationDestination.objects.create(name='A', kind='teams', webhook_url='https://a.example/x')
        page = client.get('/plugins/dependencies/graph/').content.decode()
        self.assertIn('href="/plugins/dependencies/settings/"', page)
        self.assertEqual(client.get('/plugins/dependencies/settings/').status_code, 200)
        self.assertEqual(client.get('/plugins/dependencies/settings/destinations/add/').status_code, 200)


class RecentNotificationsTests(Base):
    """The "Recent notifications" list: pagination and filters."""

    def setUp(self):
        now = timezone.now()
        self.teams = NotificationDestination.objects.create(name='Teams', kind='teams', webhook_url='https://t.example/h')
        # 14 ended episodes of core / sw1 alternately + 1 ongoing + 1 whose message was never delivered
        for i in range(14):
            OutageNotification.objects.create(device=self.devs['core' if i % 2 else 'sw1'], started_at=now - timedelta(days=30 - i),
                                              resolved_at=now - timedelta(days=29 - i))
        self.ongoing = OutageNotification.objects.create(device=self.devs['ap'], started_at=now - timedelta(hours=1))
        self.broken = OutageNotification.objects.create(device=self.devs['sw2'], started_at=now - timedelta(days=20),
                                                        resolved_at=now - timedelta(days=19))
        NotificationDelivery.objects.create(episode=self.broken, destination=self.teams, kind='outage', attempts=3, last_error='HTTP 500')
        NotificationDelivery.objects.create(episode=self.ongoing, destination=self.teams, kind='outage', attempts=1, sent_at=now)
        from django.contrib.auth import get_user_model
        from django.test import Client
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('rn', 'rn@example.com', 'x'))

    def page(self, query=''):
        return self.client.get('/plugins/dependencies/settings/' + query).content.decode()

    def test_pagination(self):
        self.assertIn('Showing 1-10 of 16', self.page('?per_page=10'))
        self.assertIn('Showing 11-16 of 16', self.page('?per_page=10&page=2'))
        self.assertIn('Showing 1-10 of 16', self.page('?per_page=10&page=nonsense'))      # an invalid page does not break it

    def test_filters(self):
        self.assertIn('Showing 1-1 of 1', self.page('?state=ongoing'))
        self.assertIn('ap', self.page('?state=ongoing'))
        self.assertIn('of 15', self.page('?state=ended&per_page=10'))
        problems = self.page('?state=problems')
        self.assertIn('Showing 1-1 of 1', problems)
        self.assertIn('failed', problems)
        self.assertIn('Showing 1-7 of 7', self.page('?q=core'))           # seven episodes of the device "core"
        self.assertIn('No notification matches the filter', self.page('?q=nothing-like-this'))

class PurgeTests(Base):
    def test_old_episodes_and_their_deliveries_are_purged(self):
        now = timezone.now()
        destination = NotificationDestination.objects.create(name='T', kind='teams', webhook_url='https://t.example/x')
        old = OutageNotification.objects.create(device=self.devs['core'], started_at=now - timedelta(days=200),
                                                resolved_at=now - timedelta(days=100))
        recent = OutageNotification.objects.create(device=self.devs['core'], started_at=now - timedelta(days=5),
                                                   resolved_at=now - timedelta(days=2))
        ongoing = OutageNotification.objects.create(device=self.devs['sw1'], started_at=now - timedelta(days=300))
        NotificationDelivery.objects.create(episode=old, destination=destination, kind='outage', sent_at=now)
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda p, k: 0):
            self.assertEqual(n.purge_old_notifications(), 0)             # 0 = never delete
        with mock.patch.object(n, 'get_plugin_config', side_effect=lambda p, k: 90):
            self.assertEqual(n.purge_old_notifications(), 1)
        self.assertEqual(set(OutageNotification.objects.values_list('pk', flat=True)), {recent.pk, ongoing.pk})
        self.assertFalse(NotificationDelivery.objects.exists())
