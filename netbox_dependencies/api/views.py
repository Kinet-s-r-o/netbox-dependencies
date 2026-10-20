from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from dcim.models import Device
from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import CustomMapFilterSet, DependencyFilterSet
from ..impact import get_impact
from ..models import CustomMap, Dependency, DeviceStatus, PollerHeartbeat
from ..status import (
    analyze_outages,
    apply_icmp_report,
    poll_targets,
    poller_config,
    poller_settings,
    status_snapshot,
    suppressed_info,
    zabbix_covered_device_ids,
)
from .serializers import CustomMapSerializer, DependencySerializer


MAX_ROOT_CAUSES = 200
MAX_EXPLAINED = 500


def _limit(request, default, maximum):
    """?limit=N (1..maximum); an invalid value = default."""
    try:
        return max(1, min(int(request.query_params.get('limit', default)), maximum))
    except ValueError:
        return default


class DependencyViewSet(NetBoxModelViewSet):
    queryset = Dependency.objects.select_related('upstream', 'downstream').prefetch_related('tags')
    serializer_class = DependencySerializer
    filterset_class = DependencyFilterSet


class CustomMapViewSet(NetBoxModelViewSet):
    queryset = CustomMap.objects.select_related('owner').prefetch_related('devices', 'tags')
    serializer_class = CustomMapSerializer
    filterset_class = CustomMapFilterSet


class ImpactAPIView(APIView):
    """GET /api/plugins/dependencies/impact/?device=<id>&device=<id>

    Returns the devices affected by an outage of the given devices."""

    queryset = Dependency.objects.none()  # permission handling v NetBoxe

    def get(self, request):
        ids = request.query_params.getlist('device')
        try:
            ids = [int(i) for i in ids]
        except ValueError:
            return Response({'detail': 'device must be an integer ID.'}, status=400)
        if not ids:
            return Response({'detail': 'At least one device parameter is required.'}, status=400)
        failed = Device.objects.restrict(request.user, 'view').filter(pk__in=ids)
        rows, summary = get_impact(failed.values_list('pk', flat=True))
        limit = _limit(request, default=1000, maximum=5000)
        return Response({
            'failed': [{'id': d.pk, 'name': d.name} for d in failed],
            'summary': summary,
            'affected_total': len(rows),
            'truncated': len(rows) > limit,
            'affected': [
                {
                    'id': r['device'].pk,
                    'name': r['device'].name,
                    'site': r['device'].site.name if r['device'].site else None,
                    'state': r['state'],
                    'depth': r['depth'],
                }
                for r in rows[:limit]
            ],
        })


class StatusTargetsAPIView(APIView):
    """GET /api/plugins/dependencies/status/targets/

    The devices the ICMP poller should ping (active, with a primary IP, without an active host in Zabbix and
    without the exclusion tag) and the poller settings configured in the UI, so the poller needs no restart to
    pick up a change. `?poller=<name>` selects the registered poller (its scope and overrides); `?in_graph=1|0` overrides
    the "only devices in the dependency tree" setting."""

    queryset = DeviceStatus.objects.none()

    def get(self, request):
        cfg = poller_config()
        flag = request.query_params.get('in_graph')
        only_in_graph = None if flag is None else flag in ('1', 'true', 'True')
        name = request.query_params.get('poller')
        settings, _entry = poller_settings(name, cfg)
        if not settings['enabled']:
            return Response({'count': 0, 'targets': [], 'settings': settings})
        visible = set(Device.objects.restrict(request.user, 'view').values_list('pk', flat=True))
        targets = [{'id': d.pk, 'name': d.name, 'ip': ip}
                   for d, ip in poll_targets(cfg, only_in_graph, poller=name) if d.pk in visible]
        return Response({'count': len(targets), 'targets': targets, 'settings': settings})


class StatusReportAPIView(APIView):
    """POST /api/plugins/dependencies/status/report/

    Body: {"results": [{"device": 12, "reachable": true, "detail": "rtt 3 ms"}, ...],
           "poller": {"name": "poller-1", "cycle_seconds": 4.2, "targets": 120, "reachable": 118, "unreachable": 2,
                      "paused": false, "version": "1.4.0"}}      ("poller" is optional: the heartbeat of the poller)
    Requires the change permission on device status."""

    queryset = DeviceStatus.objects.none()

    @staticmethod
    def _heartbeat(info):
        """Records the heartbeat of a poller; malformed values are ignored rather than rejected."""
        def number(key, cast, default=0):
            try:
                return cast(info.get(key, default))
            except (TypeError, ValueError):
                return default
        name = str(info.get('name') or 'poller')[:100]
        now = timezone.now()
        heartbeat, _ = PollerHeartbeat.objects.get_or_create(name=name, defaults={'first_seen': now, 'last_seen': now})
        heartbeat.last_seen = now
        heartbeat.cycle_seconds = number('cycle_seconds', float, None)
        heartbeat.targets = max(0, number('targets', int))
        heartbeat.reachable = max(0, number('reachable', int))
        heartbeat.unreachable = max(0, number('unreachable', int))
        heartbeat.paused = bool(info.get('paused'))
        heartbeat.version = str(info.get('version') or '')[:40]
        heartbeat.save()

    def post(self, request):
        if not request.user.has_perm('netbox_dependencies.change_devicestatus'):
            return Response({'detail': 'Missing change_devicestatus permission.'}, status=403)
        body = request.data if isinstance(request.data, dict) else {}
        results = body.get('results', [] if isinstance(body.get('poller'), dict) else None)
        if not isinstance(results, list):
            return Response({'detail': '"results" must be a list.'}, status=400)
        cfg = poller_config()
        covered = zabbix_covered_device_ids()
        ids = [r['device'] for r in results if isinstance(r, dict) and isinstance(r.get('device'), int)]
        known = set(Device.objects.filter(pk__in=ids).values_list('pk', flat=True))
        updated = changed = skipped = 0
        for r in results:
            if not isinstance(r, dict) or r.get('device') not in known or 'reachable' not in r:
                skipped += 1
                continue
            if r['device'] in covered:
                skipped += 1  # the device is monitored by Zabbix — it is the source of truth
                continue
            updated += 1
            if apply_icmp_report(r['device'], bool(r['reachable']), str(r.get('detail') or ''), cfg):
                changed += 1
        if isinstance(body.get('poller'), dict):
            self._heartbeat(body['poller'])
        return Response({'updated': updated, 'changed': changed, 'skipped': skipped})


class OutagesAPIView(APIView):
    """GET /api/plugins/dependencies/outages/ — current outages (root causes + impact)."""

    queryset = DeviceStatus.objects.none()

    def get(self, request):
        outages, explained = analyze_outages()
        return Response({
            'root_causes_total': len(outages),
            'explained_total': len(explained),
            'truncated': len(outages) > MAX_ROOT_CAUSES or len(explained) > MAX_EXPLAINED,
            'root_causes': [
                {
                    'id': o['device'].pk,
                    'name': o['device'].name,
                    'since': o['status'].last_changed,
                    'source': o['status'].source,
                    'detail': o['status'].detail,
                    'down_count': o['down_count'],
                    'degraded_count': o['degraded_count'],
                }
                for o in outages[:MAX_ROOT_CAUSES]
            ],
            'explained': [
                {'id': e['device'].pk, 'name': e['device'].name,
                 'caused_by': [c.name for c in e['caused_by']]}
                for e in explained[:MAX_EXPLAINED]
            ],
            # devices that are out of the analysis right now (see Dependencies -> Suppressed devices)
            'suppressed': [{'device': pk, **info} for pk, info in suppressed_info().items()],
        })


class StatesAPIView(APIView):
    """GET /api/plugins/dependencies/status/states/ — current state of all devices
    and the root causes of outages (for the live refresh of the graph)."""

    queryset = DeviceStatus.objects.none()

    def get(self, request):
        states, roots = status_snapshot()
        return Response({'updated': timezone.now(), 'states': states, 'roots': roots, 'suppressed': suppressed_info()})
