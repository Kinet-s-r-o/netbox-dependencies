"""Sources of device status (Zabbix, ICMP poller) and analysis of live outages."""
import re
from datetime import timedelta

from django.apps import apps
from django.db.models import Q
from django.utils import timezone

from dcim.models import Device
from netbox.plugins import get_plugin_config

from .choices import SourceChoices, StateChoices
from .graph import compute_impact
from .impact import load_edges
from .models import DeviceStatus, PollerSettings

PLUGIN = 'netbox_dependencies'


def _setting(key):
    return get_plugin_config(PLUGIN, key)


POLLER_DEFAULTS = {
    'enabled': True, 'interval_seconds': 60, 'ping_count': 3, 'ping_timeout': 1.0, 'concurrency': 500,
    'only_in_graph': True, 'exclude_tag': 'no-ping', 'alert_enabled': True, 'alert_after_seconds': 300,
}


def poller_config():
    """Effective ICMP poller settings: the row edited in the UI wins; without it the defaults apply
    (the failure threshold and the staleness limit fall back to PLUGINS_CONFIG)."""
    cfg = dict(POLLER_DEFAULTS, fail_threshold=_setting('fail_threshold'), stale_after_seconds=_setting('status_stale_after'))
    row = PollerSettings.objects.first()
    if row:
        cfg.update({key: getattr(row, key) for key in cfg})
    return cfg


def poll_targets(cfg=None, only_in_graph=None):
    """Devices the poller should ping: active, with a primary IP, not carrying the exclusion tag
    and without an active host in Zabbix. Returns a list of (device, ip)."""
    cfg = cfg or poller_config()
    if only_in_graph is None:
        only_in_graph = cfg['only_in_graph']
    devices = Device.objects.filter(status='active').filter(Q(primary_ip4__isnull=False) | Q(primary_ip6__isnull=False))
    if only_in_graph:
        devices = devices.filter(Q(upstream_dependencies__isnull=False) | Q(downstream_dependencies__isnull=False)).distinct()
    if cfg['exclude_tag']:
        devices = devices.exclude(tags__slug=cfg['exclude_tag'])
    covered = zabbix_covered_device_ids()
    result = []
    for device in devices.select_related('primary_ip4', 'primary_ip6'):
        if device.pk not in covered:
            result.append((device, str((device.primary_ip4 or device.primary_ip6).address.ip)))
    return result


def zabbix_available():
    return apps.is_installed('netbox_zabbix_status')


def zabbix_covered_device_ids():
    """Devices that have an active (enabled) host in Zabbix — the ICMP poller skips them."""
    if not zabbix_available():
        return set()
    from netbox_zabbix_status.choices import ZabbixHostStatusChoices
    from netbox_zabbix_status.models import ZabbixHost
    return set(
        ZabbixHost.objects.filter(
            device__isnull=False, status=ZabbixHostStatusChoices.ENABLED
        ).values_list('device_id', flat=True)
    )


def _set_state(device_id, state, source, detail, fail_count, now):
    """Writes the state; last_changed changes only when the state changes."""
    existing = DeviceStatus.objects.filter(device_id=device_id).first()
    if existing is None:
        DeviceStatus.objects.create(
            device_id=device_id, state=state, source=source, detail=detail[:500],
            fail_count=fail_count, last_checked=now, last_changed=now,
        )
        return True
    changed = existing.state != state
    existing.state = state
    existing.source = source
    existing.detail = detail[:500]
    existing.fail_count = fail_count
    existing.last_checked = now
    if changed:
        existing.last_changed = now
    existing.save()
    return changed


def apply_icmp_report(device_id, reachable, detail='', cfg=None):
    """Processes one ping result. A device is marked DOWN only after
    `fail_threshold` consecutive failures (protection against flapping)."""
    now = timezone.now()
    threshold = (cfg or poller_config())['fail_threshold']
    existing = DeviceStatus.objects.filter(device_id=device_id).first()
    if reachable:
        return _set_state(device_id, StateChoices.UP, SourceChoices.ICMP, detail, 0, now)
    fails = (existing.fail_count if existing and existing.source == SourceChoices.ICMP else 0) + 1
    if fails >= threshold:
        state = StateChoices.DOWN
    elif existing and existing.source == SourceChoices.ICMP:
        state = existing.state  # keeps the previous state for now
    else:
        state = StateChoices.UNKNOWN
    return _set_state(
        device_id, state, SourceChoices.ICMP,
        detail or f'ICMP failed ({fails}/{threshold})', fails, now,
    )


def sync_from_zabbix():
    """Overwrites the state of devices matched with Zabbix. DOWN = the host has an active
    (unsuppressed) problem whose name matches `zabbix_down_patterns`."""
    if not zabbix_available():
        return {'skipped': 'netbox_zabbix_status is not installed'}
    from netbox_zabbix_status.choices import ZabbixHostStatusChoices
    from netbox_zabbix_status.models import ZabbixHost

    patterns = [re.compile(p, re.IGNORECASE) for p in _setting('zabbix_down_patterns')]
    stale_after = timedelta(seconds=_setting('zabbix_stale_after'))
    now = timezone.now()

    verdicts = {}  # device_id -> (state, detail); DOWN > UNKNOWN > UP
    rank = {StateChoices.UP: 0, StateChoices.UNKNOWN: 1, StateChoices.DOWN: 2}
    hosts = ZabbixHost.objects.filter(
        device__isnull=False, status=ZabbixHostStatusChoices.ENABLED
    ).prefetch_related('problems')
    for host in hosts:
        if host.in_maintenance:
            verdict = (StateChoices.UNKNOWN, 'Zabbix host in maintenance')
        elif host.last_synced is None or now - host.last_synced > stale_after:
            verdict = (StateChoices.UNKNOWN, 'Zabbix data is stale')
        else:
            matched = [
                p.name for p in host.problems.all()
                if not p.suppressed and any(rx.search(p.name) for rx in patterns)
            ]
            verdict = (StateChoices.DOWN, matched[0]) if matched else (StateChoices.UP, '')
        current = verdicts.get(host.device_id)
        if current is None or rank[verdict[0]] > rank[current[0]]:
            verdicts[host.device_id] = verdict

    changed = 0
    for device_id, (state, detail) in verdicts.items():
        if _set_state(device_id, state, SourceChoices.ZABBIX, detail, 0, now):
            changed += 1
    # Devices that Zabbix no longer covers must not keep a stale Zabbix state
    removed, _ = DeviceStatus.objects.filter(source=SourceChoices.ZABBIX).exclude(
        device_id__in=verdicts.keys()
    ).delete()
    return {'devices': len(verdicts), 'changed': changed, 'removed': removed}


def down_device_ids():
    """IDs of devices in the DOWN state whose source is not stale."""
    cutoff = timezone.now() - timedelta(seconds=poller_config()['stale_after_seconds'])
    return set(
        DeviceStatus.objects.filter(state=StateChoices.DOWN, last_checked__gte=cutoff)
        .values_list('device_id', flat=True)
    )


def analyze_outages():
    """Splits DOWN devices into root causes and those explained by an outage
    of an upstream device.

    Returns (outages, explained): outages = [{'device', 'since', 'detail',
    'affected_count', 'down_count'}] and explained = [{'device', 'caused_by'}]."""
    down_ids = down_device_ids()
    if not down_ids:
        return [], []
    edges = load_edges()
    roots, explained_ids = [], set()
    for device_id in down_ids:
        others = down_ids - {device_id}
        impact = compute_impact(edges, others)
        if device_id in impact and impact[device_id].state == 'down':
            explained_ids.add(device_id)
        else:
            roots.append(device_id)

    devices = {d.pk: d for d in Device.objects.filter(pk__in=down_ids).select_related('site', 'role')}
    statuses = {s.device_id: s for s in DeviceStatus.objects.filter(device_id__in=down_ids)}

    root_impacts = {root: compute_impact(edges, {root}) for root in roots}
    outages = [
        {
            'device': devices[root],
            'status': statuses[root],
            'down_count': sum(1 for i in impact.values() if i.state == 'down'),
            'degraded_count': sum(1 for i in impact.values() if i.state == 'degraded'),
        }
        for root, impact in root_impacts.items()
    ]
    outages.sort(key=lambda o: (-o['down_count'], o['device'].name or ''))

    explained = [
        {
            'device': devices[device_id],
            'status': statuses[device_id],
            'caused_by': [devices[r] for r, impact in root_impacts.items() if device_id in impact],
        }
        for device_id in explained_ids
    ]
    explained.sort(key=lambda e: e['device'].name or '')
    return outages, explained


def status_snapshot():
    """State of all devices for the live refresh of the graph.

    Returns (states, roots): states = {device_id: [state, source, detail, last_changed_iso]}
    (a stale record is reported as 'unknown'), roots = root causes of outages
    [{'id', 'name', 'down_count', 'degraded_count'}]."""
    cutoff = timezone.now() - timedelta(seconds=poller_config()['stale_after_seconds'])
    states = {}
    for device_id, state, source, detail, changed, checked in DeviceStatus.objects.values_list(
        'device_id', 'state', 'source', 'detail', 'last_changed', 'last_checked'
    ):
        if checked < cutoff:
            state, detail = StateChoices.UNKNOWN, 'Status is stale'
        states[device_id] = [state, source, detail, changed.isoformat()]
    outages, _explained = analyze_outages()
    roots = [
        {
            'id': o['device'].pk,
            'name': o['device'].name,
            'down_count': o['down_count'],
            'degraded_count': o['degraded_count'],
        }
        for o in outages
    ]
    return states, roots
