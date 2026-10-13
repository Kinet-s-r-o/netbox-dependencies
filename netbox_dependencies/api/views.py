from django.db.models import Q
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from dcim.models import Device
from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import DependencyFilterSet
from ..impact import get_impact
from ..models import Dependency, DeviceStatus
from ..status import analyze_outages, apply_icmp_report, status_snapshot, zabbix_covered_device_ids
from .serializers import DependencySerializer


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

    Devices the ICMP poller should ping: active, with a primary IP and without an
    active host in Zabbix. `?in_graph=1` = only devices in the dependency tree."""

    queryset = DeviceStatus.objects.none()

    def get(self, request):
        devices = Device.objects.restrict(request.user, 'view').filter(
            status='active'
        ).filter(Q(primary_ip4__isnull=False) | Q(primary_ip6__isnull=False))
        if request.query_params.get('in_graph') in ('1', 'true', 'True'):
            devices = devices.filter(
                Q(upstream_dependencies__isnull=False) | Q(downstream_dependencies__isnull=False)
            ).distinct()
        covered = zabbix_covered_device_ids()
        targets = []
        for d in devices.select_related('primary_ip4', 'primary_ip6'):
            if d.pk in covered:
                continue
            ip = d.primary_ip4 or d.primary_ip6
            targets.append({'id': d.pk, 'name': d.name, 'ip': str(ip.address.ip)})
        return Response({'count': len(targets), 'targets': targets})


class StatusReportAPIView(APIView):
    """POST /api/plugins/dependencies/status/report/

    Body: {"results": [{"device": 12, "reachable": true, "detail": "rtt 3 ms"}, ...]}
    Requires the add + change permissions on device status."""

    queryset = DeviceStatus.objects.none()

    def post(self, request):
        if not request.user.has_perm('netbox_dependencies.change_devicestatus'):
            return Response({'detail': 'Missing change_devicestatus permission.'}, status=403)
        results = request.data.get('results') if isinstance(request.data, dict) else None
        if not isinstance(results, list):
            return Response({'detail': '"results" must be a list.'}, status=400)
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
            if apply_icmp_report(r['device'], bool(r['reachable']), str(r.get('detail') or '')):
                changed += 1
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
        })


class StatesAPIView(APIView):
    """GET /api/plugins/dependencies/status/states/ — current state of all devices
    and the root causes of outages (for the live refresh of the graph)."""

    queryset = DeviceStatus.objects.none()

    def get(self, request):
        states, roots = status_snapshot()
        return Response({'updated': timezone.now(), 'states': states, 'roots': roots})
