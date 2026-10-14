"""Notifications about outages and their end for several destinations (Teams, Slack, generic webhook).

One message is sent per ROOT-CAUSE outage (not per affected device):
  * new outage — the device is DOWN, the outage is not explained by an outage of an upstream device
    and it lasts at least `delay_seconds`,
  * outage ended — the device is UP again (only destinations that received the outage message are told).
Every destination keeps its own delivery records (NotificationDelivery): if one destination fails, only it is retried.
The message format per destination type lives in channels.py."""
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from netbox.plugins import get_plugin_config

from . import channels
from .choices import DestinationKindChoices, StateChoices
from .impact import get_impact
from .models import (
    Dependency,
    DeviceStatus,
    PollerHeartbeat,
    NotificationDelivery,
    NotificationDestination,
    NotificationSettings,
    OutageNotification,
)
from .status import analyze_outages, poller_config

logger = logging.getLogger('netbox_dependencies.notifications')

PLUGIN = 'netbox_dependencies'
MAX_ATTEMPTS = 10                       # after this many failed attempts a message is no longer retried for that destination
RECOVERY_WINDOW = timedelta(hours=24)   # a recovery message that could not be sent is retried for one day at most


class DeliveryError(Exception):
    pass


# ------------------------------------------------------------------ settings
# keys in PLUGINS_CONFIG (env) -> keys in the effective settings
_CONFIG_KEYS = {
    'notifications_enabled': 'enabled',
    'notification_delay_seconds': 'delay_seconds',
    'notify_recovery': 'notify_recovery',
    'public_url': 'public_url',
}


def effective_settings():
    """General settings: values saved in the UI take precedence over env (PLUGINS_CONFIG);
    an empty public URL in the UI = use the value from env."""
    cfg = {name: get_plugin_config(PLUGIN, key) for key, name in _CONFIG_KEYS.items()}
    row = NotificationSettings.objects.first()
    if row is not None:
        cfg['enabled'] = row.enabled
        cfg['delay_seconds'] = row.delay_seconds
        cfg['notify_recovery'] = row.notify_recovery
        if row.public_url:
            cfg['public_url'] = row.public_url
    return cfg


def _setting(key):
    return effective_settings()[key]


def seed_from_environment():
    """A webhook from the legacy env setting (`DEPENDENCIES_TEAMS_WEBHOOK_URL`) is taken over once as the
    destination "Teams". The settings row is created immediately, so the env value does not come back after the destination is deleted."""
    url = get_plugin_config(PLUGIN, 'teams_webhook_url')
    if not url or NotificationSettings.objects.exists() or NotificationDestination.objects.exists():
        return None
    NotificationSettings.objects.create(
        enabled=get_plugin_config(PLUGIN, 'notifications_enabled'),
        delay_seconds=get_plugin_config(PLUGIN, 'notification_delay_seconds'),
        notify_recovery=get_plugin_config(PLUGIN, 'notify_recovery'))
    return NotificationDestination.objects.create(name='Teams', kind=DestinationKindChoices.TEAMS, webhook_url=url)


def mask_url(url):
    """The webhook URL contains a signature (sig=...): the UI shows only the server address and the last characters."""
    if not url:
        return ''
    parts = urllib.parse.urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}/…{url[-6:]}' if parts.netloc else '…'


# ------------------------------------------------------------------ sending
def _post(url, payload, secret=''):
    """POST JSON; 3 attempts. With `secret` the body is signed (HMAC-SHA256, header X-Signature-256).
    The URL is never written to the log or to the error."""
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json; charset=utf-8', 'User-Agent': 'netbox-dependencies'}
    if secret:
        headers['X-Signature-256'] = 'sha256=' + hmac.new(secret.encode('utf-8'), data, hashlib.sha256).hexdigest()
    request = urllib.request.Request(url, data=data, method='POST', headers=headers)
    last = 'unknown error'
    for pause in (0, 1, 3):
        if pause:
            time.sleep(pause)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if 200 <= response.status < 300:
                    return
                last = f'HTTP {response.status}'
        except urllib.error.HTTPError as exc:
            last = f'HTTP {exc.code}'
        except Exception as exc:  # network, DNS, TLS, timeout
            last = type(exc).__name__
    raise DeliveryError(last)


def send_to(destination, payload):
    """Sends the payload to one destination and records the result (shown in the UI)."""
    secret = destination.secret if destination.kind == DestinationKindChoices.GENERIC else ''
    destination.last_attempt_at = timezone.now()
    try:
        _post(destination.webhook_url, payload, secret=secret)
    except DeliveryError as exc:
        destination.last_ok, destination.last_error = False, str(exc)[:300]
        destination.save(update_fields=['last_attempt_at', 'last_ok', 'last_error'])
        raise
    destination.last_ok, destination.last_error = True, ''
    destination.save(update_fields=['last_attempt_at', 'last_ok', 'last_error'])


# ------------------------------------------------------------------ display models
def _base_url():
    return (_setting('public_url') or '').rstrip('/')


def _device_url(device):
    base = _base_url()
    return f'{base}{device.get_absolute_url()}' if base else ''


def _graph_url(device):
    base = _base_url()
    if not base:
        return ''
    return f"{base}{reverse('plugins:netbox_dependencies:graph')}?site={device.site_id}&focus={device.pk}&impact=1"


def _outage_view(episode):
    device = episode.device
    status = getattr(device, 'dependency_status', None)
    rows, summary = get_impact([device.pk])
    return {
        'device_id': device.pk, 'name': device.name, 'site': device.site.name, 'since': episode.started_at,
        'down': summary['down'], 'degraded': summary['degraded'],
        'names': [r['device'].name + (' (degraded)' if r['state'] == 'degraded' else '') for r in rows],
        'source': ((status.get_source_display() if status else '—')
                   + (f' — {status.detail}' if status and status.detail else '')),
        'affected_pks': {r['device'].pk for r in rows if r['state'] == 'down'} | {device.pk},
        'graph_url': _graph_url(device), 'device_url': _device_url(device),
    }


def _recovery_view(episode):
    device = episode.device
    return {'device_id': device.pk, 'name': device.name, 'site': device.site.name, 'since': episode.started_at,
            'until': episode.resolved_at, 'down': episode.peak_down, 'degraded': episode.peak_degraded,
            'graph_url': _graph_url(device), 'device_url': _device_url(device)}


def sample_payloads(destination_kind):
    """Sample outage and recovery for the given destination type; made-up devices, marked as a test."""
    base = _base_url()
    home = f"{base}{reverse('plugins:netbox_dependencies:graph')}" if base else ''
    payloads = channels.render_samples(destination_kind, timezone.now(), home)
    for payload in payloads:
        payload['text'] = '🧪 Sample: ' + payload['text']
    return payloads


def send_samples(destination):
    """Sends both sample messages (outage + recovery) to the destination. Raises DeliveryError on failure."""
    for payload in sample_payloads(destination.kind):
        send_to(destination, payload)


# ------------------------------------------------------------------ cleanup
def purge_old_notifications(now=None):
    """Deletes episodes (and their deliveries) whose outage ended more than `notification_retention_days`
    days ago (0 = never delete). Open (ongoing) outages are never deleted. Returns the number of deleted episodes."""
    days = int(get_plugin_config(PLUGIN, 'notification_retention_days'))
    if days <= 0:
        return 0
    cutoff = (now or timezone.now()) - timedelta(days=days)
    deleted, per_model = OutageNotification.objects.filter(resolved_at__lt=cutoff).delete()
    return per_model.get(OutageNotification._meta.label, 0) if deleted else 0


# ------------------------------------------------------------------ main logic
def _deliver(destination, kind, episodes, now, result_key, result):
    """Sends one message (outage / recovery) to a destination for all waiting episodes and records the delivery result."""
    deliveries = list(NotificationDelivery.objects.filter(
        destination=destination, kind=kind, episode__in=episodes))
    if not deliveries:
        return
    by_episode = {d.episode_id: d for d in deliveries}
    ordered = [e for e in episodes if e.pk in by_episode]
    if kind == NotificationDelivery.KIND_OUTAGE:
        payload = channels.render_outage(destination.kind, [_outage_view(e) for e in ordered], now)
    else:
        payload = channels.render_recovery(destination.kind, [_recovery_view(e) for e in ordered], now)
    try:
        send_to(destination, payload)
    except DeliveryError as exc:
        for d in deliveries:
            d.attempts += 1
            d.last_error = str(exc)[:300]
            d.save(update_fields=['attempts', 'last_error'])
        result['errors'] += 1
        logger.warning('Notification to "%s" (%s) failed: %s', destination.name, kind, exc)
        return
    for d in deliveries:
        d.attempts += 1
        d.sent_at, d.last_error = now, ''
        d.save(update_fields=['attempts', 'sent_at', 'last_error'])
    result[result_key] += len(deliveries)


def run_notifications(now=None):
    """Called from the minute job. Returns the summary {'sent', 'resolved', 'errors'} (delivery counts) or {'skipped': reason}."""
    if not _setting('enabled'):
        return {'skipped': 'Notifications are disabled'}
    seed_from_environment()
    destinations = list(NotificationDestination.objects.filter(enabled=True))
    if not destinations:
        return {'skipped': 'No enabled notification destination'}
    now = now or timezone.now()
    delay = timedelta(seconds=int(_setting('delay_seconds')))

    outages, _explained = analyze_outages()
    # Only devices that take part in a dependency are announced; a standalone device that is down
    # (monitored by Zabbix but not modelled here) is none of this plugin's business.
    in_graph = (set(Dependency.objects.values_list('upstream_id', flat=True))
                | set(Dependency.objects.values_list('downstream_id', flat=True)))
    roots = {o['device'].pk: o for o in outages if o['device'].pk in in_graph}
    open_episodes = {
        e.device_id: e for e in OutageNotification.objects.filter(resolved_at__isnull=True, device_id__in=in_graph)
        .select_related('device__site')
    }

    # --- episodes: new root-cause outages
    for pk, outage in roots.items():
        episode = open_episodes.get(pk)
        if episode is None:
            if now - outage['status'].last_changed < delay:
                continue   # not down long enough yet — possibly a short outage
            episode = OutageNotification.objects.create(
                device=outage['device'], started_at=outage['status'].last_changed,
                peak_down=outage['down_count'], peak_degraded=outage['degraded_count'])
            episode.device = outage['device']
            open_episodes[pk] = episode
        else:
            episode.peak_down = max(episode.peak_down, outage['down_count'])
            episode.peak_degraded = max(episode.peak_degraded, outage['degraded_count'])
            episode.save(update_fields=['peak_down', 'peak_degraded'])

    # --- episodes: ended outages (the device is UP again; if it is merely "explained" by an upstream outage, the episode stays open)
    for pk, episode in list(open_episodes.items()):
        if pk in roots:
            continue
        status = DeviceStatus.objects.filter(device_id=pk).first()
        if status and status.state == StateChoices.UP:
            episode.resolved_at = status.last_changed if status.last_changed > episode.started_at else now
            episode.save(update_fields=['resolved_at'])
            del open_episodes[pk]

    # --- deliveries per destination
    result = {'sent': 0, 'resolved': 0, 'errors': 0}
    notify_recovery = _setting('notify_recovery')
    recent_resolved = list(OutageNotification.objects.filter(
        resolved_at__isnull=False, resolved_at__gte=now - RECOVERY_WINDOW).select_related('device__site'))
    for destination in destinations:
        for episode in open_episodes.values():
            NotificationDelivery.objects.get_or_create(
                episode=episode, destination=destination, kind=NotificationDelivery.KIND_OUTAGE)
        pending = [e for e in open_episodes.values() if NotificationDelivery.objects.filter(
            episode=e, destination=destination, kind=NotificationDelivery.KIND_OUTAGE,
            sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS).exists()]
        if pending:
            _deliver(destination, NotificationDelivery.KIND_OUTAGE, pending, now, 'sent', result)

        if not notify_recovery:
            continue
        announced = set(NotificationDelivery.objects.filter(
            destination=destination, kind=NotificationDelivery.KIND_OUTAGE, sent_at__isnull=False,
            episode__in=recent_resolved).values_list('episode_id', flat=True))
        for episode in recent_resolved:
            if episode.pk in announced:   # a recovery is reported only where the outage message was delivered
                NotificationDelivery.objects.get_or_create(
                    episode=episode, destination=destination, kind=NotificationDelivery.KIND_RECOVERY)
        pending = [e for e in recent_resolved if NotificationDelivery.objects.filter(
            episode=e, destination=destination, kind=NotificationDelivery.KIND_RECOVERY,
            sent_at__isnull=True, attempts__lt=MAX_ATTEMPTS).exists()]
        if pending:
            _deliver(destination, NotificationDelivery.KIND_RECOVERY, pending, now, 'resolved', result)
    return result


# ------------------------------------------------------------------ health of the ICMP poller
def run_poller_alerts(now=None):
    """Called from the minute job. A poller that has not reported for `alert_after_seconds` raises one "silent" alert
    (to every enabled destination); when it reports again one "back" message follows (if recovery messages are on).
    Returns {'down', 'up', 'errors'} (alerts sent) or {'skipped': reason}."""
    cfg = poller_config()
    if not cfg['alert_enabled']:
        return {'skipped': 'Poller alerts are disabled'}
    if not _setting('enabled'):
        return {'skipped': 'Notifications are disabled'}
    destinations = list(NotificationDestination.objects.filter(enabled=True))
    if not destinations:
        return {'skipped': 'No enabled notification destination'}
    now = now or timezone.now()
    base = _base_url()
    url = f"{base}{reverse('plugins:netbox_dependencies:poller')}" if base else ''
    result = {'down': 0, 'up': 0, 'errors': 0}
    for heartbeat in PollerHeartbeat.objects.all():
        silent_for = (now - heartbeat.last_seen).total_seconds()
        silent = silent_for > cfg['alert_after_seconds']
        if silent and heartbeat.alerted_at is None:
            event = 'down'
        elif not silent and heartbeat.alerted_at is not None:
            event = 'up'
        else:
            continue
        if event == 'up' and not _setting('notify_recovery'):
            heartbeat.alerted_at = None
            heartbeat.save(update_fields=['alerted_at'])
            continue
        # for "back": the silence ran from the last report before the alert (about `alert_after_seconds` before it) to the first one after
        silence = silent_for if event == 'down' else (
            max(0.0, (heartbeat.last_seen - heartbeat.alerted_at).total_seconds()) + cfg['alert_after_seconds'])
        view = {'name': heartbeat.name, 'last_seen': heartbeat.last_seen, 'silent_seconds': silence, 'url': url}
        delivered = False
        for destination in destinations:
            try:
                send_to(destination, channels.render_poller(destination.kind, event, view, now))
                delivered = True
            except DeliveryError as exc:
                result['errors'] += 1
                logger.warning('Poller alert to "%s" failed: %s', destination.name, exc)
        if delivered:   # nothing reached any destination: try again next minute
            heartbeat.alerted_at = now if event == 'down' else None
            heartbeat.save(update_fields=['alerted_at'])
            result[event] += 1
    return result
