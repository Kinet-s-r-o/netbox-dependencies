import csv
import json
import re
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef
from django.http import FileResponse, HttpResponse, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import View

from dcim.models import Device
from netbox.object_actions import AddObject, BulkDelete, BulkEdit, BulkExport, BulkImport, BulkRename
from netbox.plugins import get_plugin_config
from netbox.views import generic
from utilities.paginator import get_paginate_count
from utilities.views import ViewTab, register_model_view

from . import tables
from .choices import DestinationKindChoices
from .filtersets import CustomMapFilterSet, DependencyFilterSet, PeerLinkFilterSet
from .forms import (
    CustomMapFilterForm,
    CustomMapForm,
    DependencyFilterForm,
    DependencyForm,
    DependencyAddParentsForm,
    DependencyImportForm,
    DependencyMultiAddForm,
    DestinationForm,
    DeviceSuppressionForm,
    ImpactForm,
    NotificationSettingsForm,
    PeerLinkFilterForm,
    PeerLinkForm,
    PeerLinkImportForm,
    PollerForm,
    PollerSettingsForm,
)
from .graph_data import build_graph, build_sidebar
from .impact import get_impact
from .models import (
    CustomMap,
    Dependency,
    DeviceStatus,
    DeviceSuppression,
    NodePosition,
    NotificationDelivery,
    NotificationDestination,
    NotificationSettings,
    OutageNotification,
    PeerLink,
    Poller,
    PollerHeartbeat,
    PollerSettings,
)
from .notifications import DeliveryError, effective_settings, mask_url, seed_from_environment, send_samples
from .pagination import paginate
from .status import PRESETS, analyze_outages, poll_targets, poller_config, poller_settings, round_estimate
from .tree import build_tree


class DependencyBulkExport(BulkExport):
    """NetBox's Export dropdown with one more entry: the list as a CSV the Import page reads again."""
    template_name = 'netbox_dependencies/buttons/export.html'

    @classmethod
    def get_context(cls, context, model):
        data = super().get_context(context, model)
        data['import_ready_url'] = reverse('plugins:netbox_dependencies:dependency_export_import')
        return data


@register_model_view(Dependency, 'list', path='', detail=False)
class DependencyListView(generic.ObjectListView):
    queryset = Dependency.objects.select_related('upstream', 'downstream')
    table = tables.DependencyTable
    filterset = DependencyFilterSet
    filterset_form = DependencyFilterForm
    actions = (AddObject, BulkImport, DependencyBulkExport, BulkEdit, BulkRename, BulkDelete)


@register_model_view(Dependency, 'export_import', path='export-for-import', detail=False)
class DependencyExportForImportView(PermissionRequiredMixin, View):
    """CSV of the (filtered) dependencies that the Import page accepts again: devices by name, choices by value, tags as slugs.
    Devices are identified by name, so two devices with the same name cannot be told apart on import."""

    permission_required = 'netbox_dependencies.view_dependency'
    COLUMNS = ('upstream', 'downstream', 'dependency_type', 'medium', 'redundancy_group', 'description', 'tags')

    def get(self, request):
        queryset = Dependency.objects.restrict(request.user, 'view').select_related('upstream', 'downstream')             .prefetch_related('tags').order_by('upstream__name', 'downstream__name')
        queryset = DependencyFilterSet(request.GET, queryset).qs
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="dependencies-{timezone.localdate():%Y%m%d}.csv"'
        writer = csv.writer(response)
        writer.writerow(self.COLUMNS)
        for dependency in queryset:
            writer.writerow([
                dependency.upstream.name, dependency.downstream.name, dependency.dependency_type, dependency.medium,
                dependency.redundancy_group, dependency.description,
                ','.join(sorted(tag.slug for tag in dependency.tags.all())),
            ])
        return response


@register_model_view(Dependency)
class DependencyView(generic.ObjectView):
    queryset = Dependency.objects.select_related('upstream', 'downstream')


@register_model_view(Dependency, 'add', detail=False)
@register_model_view(Dependency, 'edit')
class DependencyEditView(generic.ObjectEditView):
    queryset = Dependency.objects.all()
    form = DependencyForm


@register_model_view(Dependency, 'delete')
class DependencyDeleteView(generic.ObjectDeleteView):
    queryset = Dependency.objects.all()


@register_model_view(Dependency, 'bulk_import', path='import', detail=False)
class DependencyBulkImportView(generic.BulkImportView):
    queryset = Dependency.objects.all()
    model_form = DependencyImportForm


@register_model_view(Dependency, 'bulk_delete', path='delete', detail=False)
class DependencyBulkDeleteView(generic.BulkDeleteView):
    queryset = Dependency.objects.all()
    filterset = DependencyFilterSet
    table = tables.DependencyTable


@register_model_view(PeerLink, 'list', path='', detail=False)
class PeerLinkListView(generic.ObjectListView):
    queryset = PeerLink.objects.select_related('device_a', 'device_b')
    table = tables.PeerLinkTable
    filterset = PeerLinkFilterSet
    filterset_form = PeerLinkFilterForm


@register_model_view(PeerLink)
class PeerLinkView(generic.ObjectView):
    queryset = PeerLink.objects.select_related('device_a', 'device_b')


@register_model_view(PeerLink, 'add', detail=False)
@register_model_view(PeerLink, 'edit')
class PeerLinkEditView(generic.ObjectEditView):
    queryset = PeerLink.objects.all()
    form = PeerLinkForm


@register_model_view(PeerLink, 'delete')
class PeerLinkDeleteView(generic.ObjectDeleteView):
    queryset = PeerLink.objects.all()


@register_model_view(PeerLink, 'bulk_import', path='import', detail=False)
class PeerLinkBulkImportView(generic.BulkImportView):
    queryset = PeerLink.objects.all()
    model_form = PeerLinkImportForm


@register_model_view(PeerLink, 'bulk_delete', path='delete', detail=False)
class PeerLinkBulkDeleteView(generic.BulkDeleteView):
    queryset = PeerLink.objects.all()
    filterset = PeerLinkFilterSet
    table = tables.PeerLinkTable


# the tree is expanded only for a small number of devices; the device tab shows only the first N direct dependencies
TREE_EXPAND_LIMIT = 300
TAB_LIST_LIMIT = 25


def _device_tab_badge(obj):
    return (
        Dependency.objects.filter(upstream=obj).count()
        + Dependency.objects.filter(downstream=obj).count()
    )


@register_model_view(Device, name='dependencies', path='dependencies')
class DeviceDependenciesTabView(generic.ObjectView):
    """Device "Dependencies" tab: upstream and dependent devices and the impact of an outage of the device."""

    queryset = Device.objects.all()
    template_name = 'netbox_dependencies/device_tab.html'
    base_template = 'dcim/device/base.html'
    tab = ViewTab(
        label='Dependencies',
        badge=_device_tab_badge,
        hide_if_empty=False,
        permission='netbox_dependencies.view_dependency',
    )

    def get_extra_context(self, request, instance):
        upstream = Dependency.objects.filter(downstream=instance).select_related('upstream__site')
        downstream = Dependency.objects.filter(upstream=instance).select_related('downstream__site')
        rows, summary = get_impact([instance.pk])
        paginator, page = paginate(self.request, rows)
        return {
            'upstream_deps': upstream[:TAB_LIST_LIMIT],
            'upstream_total': upstream.count(),
            'downstream_deps': downstream[:TAB_LIST_LIMIT],
            'downstream_total': downstream.count(),
            'list_limit': TAB_LIST_LIMIT,
            'impact_rows': page.object_list,
            'page': page,
            'paginator': paginator,
            'impact_summary': summary,
            'base_template': self.base_template,
            'device_status': getattr(instance, 'dependency_status', None),
        }


class ImpactView(PermissionRequiredMixin, View):
    """Outage simulation for one or more devices."""

    permission_required = 'netbox_dependencies.view_dependency'
    template_name = 'netbox_dependencies/impact.html'

    def get(self, request):
        form = ImpactForm(request.GET or None)
        context = {'form': form, 'searched': False}
        if request.GET and form.is_valid():
            failed = form.cleaned_data['devices']
            rows, summary = get_impact([d.pk for d in failed])
            paginator, page = paginate(request, rows)
            context.update(searched=True, failed=failed, rows=page.object_list, summary=summary,
                           page=page, paginator=paginator)
        return render(request, self.template_name, context)


MAX_ROOTS_SHOWN = 100   # only a few root causes exist at a time in practice; the cap protects the page during a big outage


class LiveOutagesView(PermissionRequiredMixin, View):
    """Current outages based on the status from Zabbix and the ICMP poller."""

    permission_required = 'netbox_dependencies.view_dependency'
    template_name = 'netbox_dependencies/live.html'

    def get(self, request):
        outages, explained = analyze_outages()
        paginator, page = paginate(request, explained)
        now = timezone.now()
        suppressions = [x for x in DeviceSuppression.objects.select_related('device__site') if x.until is None or x.until > now]
        states = dict(DeviceStatus.objects.filter(device_id__in=[x.device_id for x in suppressions]).values_list('device_id', 'state'))
        for x in suppressions:
            x.state = states.get(x.device_id, 'none')
        return render(request, self.template_name, {
            'suppressions': suppressions,
            'outages': outages[:MAX_ROOTS_SHOWN], 'outages_total': len(outages), 'max_roots': MAX_ROOTS_SHOWN,
            'explained': page.object_list, 'explained_total': len(explained), 'page': page, 'paginator': paginator,
        })


class SuppressionListView(PermissionRequiredMixin, View):
    """Devices that are out of the outage analysis (suppressed), active and expired."""

    permission_required = 'netbox_dependencies.view_devicesuppression'
    template_name = 'netbox_dependencies/suppressions.html'

    def get(self, request):
        now = timezone.now()
        rows = list(DeviceSuppression.objects.select_related('device__site', 'created_by'))
        states = dict(DeviceStatus.objects.filter(device_id__in=[r.device_id for r in rows]).values_list('device_id', 'state'))
        for row in rows:
            row.active = row.until is None or row.until > now
            row.state = states.get(row.device_id, 'none')
        rows.sort(key=lambda r: (not r.active, (r.device.name or '').lower()))
        paginator, page = paginate(request, rows)
        return render(request, self.template_name, {
            'rows': page.object_list, 'page': page, 'paginator': paginator,
            'can_add': request.user.has_perm('netbox_dependencies.add_devicesuppression'),
            'can_change': request.user.has_perm('netbox_dependencies.change_devicesuppression'),
            'can_delete': request.user.has_perm('netbox_dependencies.delete_devicesuppression'),
        })


class SuppressionEditView(PermissionRequiredMixin, View):
    """Suppress a device (add) or change a suppression (edit). `return_url` (a path on this site) says where to go afterwards."""

    template_name = 'netbox_dependencies/suppression_form.html'

    def get_permission_required(self):
        return ('netbox_dependencies.change_devicesuppression',) if self.kwargs.get('pk') else ('netbox_dependencies.add_devicesuppression',)

    @staticmethod
    def _return_url(request):
        url = request.POST.get('return_url') or request.GET.get('return_url') or ''
        ok = url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())
        return url if ok else ''

    def _context(self, request, form, instance):
        back = self._return_url(request)
        return {'form': form, 'instance': instance, 'return_url': back,
                'cancel_url': back or reverse('plugins:netbox_dependencies:suppression_list')}

    def get(self, request, pk=None):
        instance = get_object_or_404(DeviceSuppression, pk=pk) if pk else None
        if instance:
            initial = {'device': instance.device_id, 'reason': instance.reason, 'until': instance.until,
                       'include_dependents': instance.include_dependents}
        else:
            initial = {'device': request.GET['device']} if request.GET.get('device') else {}
        return render(request, self.template_name, self._context(request, DeviceSuppressionForm(initial=initial), instance))

    def post(self, request, pk=None):
        instance = get_object_or_404(DeviceSuppression, pk=pk) if pk else None
        form = DeviceSuppressionForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, form, instance))
        device = form.cleaned_data['device']
        defaults = {'reason': form.cleaned_data['reason'], 'until': form.cleaned_data['until'],
                    'include_dependents': form.cleaned_data['include_dependents']}
        if instance and instance.device_id != device.pk:      # the device of an existing suppression is not changed here
            form.add_error('device', 'To suppress another device add a new suppression.')
            return render(request, self.template_name, self._context(request, form, instance))
        _suppression, created = DeviceSuppression.objects.update_or_create(
            device=device, defaults={**defaults, **({'created_by': request.user} if not instance else {})})
        messages.success(request, f'{device} is suppressed: it is ignored by the outage analysis and raises no notifications.')
        return redirect(self._return_url(request) or 'plugins:netbox_dependencies:suppression_list')


class SuppressionResumeView(PermissionRequiredMixin, View):
    """Ends a suppression (the device counts again). POST; answers JSON to the graph and redirects otherwise."""

    permission_required = 'netbox_dependencies.delete_devicesuppression'

    def post(self, request, pk):
        suppression = get_object_or_404(DeviceSuppression, pk=pk)
        name = suppression.device.name
        suppression.delete()
        if 'application/json' in request.headers.get('Accept', ''):
            return JsonResponse({'resumed': name})
        messages.success(request, f'{name} is monitored again.')
        url = request.POST.get('return_url') or ''
        ok = url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())
        return redirect(url if ok else 'plugins:netbox_dependencies:suppression_list')


class DependencyTreeView(PermissionRequiredMixin, View):
    """Tree view: the roots are devices without an upstream."""

    permission_required = 'netbox_dependencies.view_dependency'
    template_name = 'netbox_dependencies/tree.html'

    def get(self, request):
        roots, stats = build_tree(page_number=request.GET.get('page'), per_page=get_paginate_count(request))
        page = stats['page']
        return render(request, self.template_name, {
            'roots': roots, 'stats': stats, 'page': page, 'paginator': page.paginator,
            'expanded': stats['devices'] <= TREE_EXPAND_LIMIT,
        })


class DependencyMultiAddView(PermissionRequiredMixin, View):
    """Creates dependencies in bulk: one upstream with several downstream devices (default), or - with
    `direction=parents` - one downstream device with several upstream devices. `return_url` (a path on this
    site) says where to go afterwards; the graph uses it for its "add" shortcuts."""

    permission_required = 'netbox_dependencies.add_dependency'
    template_name = 'netbox_dependencies/add_multiple.html'

    @staticmethod
    def _direction(request):
        return 'parents' if (request.POST.get('direction') or request.GET.get('direction')) == 'parents' else 'children'

    @staticmethod
    def _return_url(request):
        url = request.POST.get('return_url') or request.GET.get('return_url') or ''
        ok = url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())
        return url if ok else ''

    def _context(self, request, form):
        direction = self._direction(request)
        return {'form': form, 'direction': direction, 'return_url': self._return_url(request),
                'cancel_url': self._return_url(request) or reverse('plugins:netbox_dependencies:tree')}

    def get(self, request):
        if self._direction(request) == 'parents':
            initial = {'downstream': request.GET['downstream']} if request.GET.get('downstream') else {}
            form = DependencyAddParentsForm(initial=initial)
        else:
            initial = {'upstream': request.GET['upstream']} if request.GET.get('upstream') else {}
            form = DependencyMultiAddForm(initial=initial)
        return render(request, self.template_name, self._context(request, form))

    def post(self, request):
        parents = self._direction(request) == 'parents'
        form = (DependencyAddParentsForm if parents else DependencyMultiAddForm)(request.POST)
        if form.is_valid():
            if parents:
                pairs = [(device, form.cleaned_data['downstream']) for device in form.cleaned_data['upstreams']]
                anchor = form.cleaned_data['downstream']
            else:
                pairs = [(form.cleaned_data['upstream'], device) for device in form.cleaned_data['downstreams']]
                anchor = form.cleaned_data['upstream']
            created, skipped, errors = 0, [], []
            try:
                with transaction.atomic():
                    for upstream, downstream in pairs:
                        other = upstream if parents else downstream
                        if Dependency.objects.filter(upstream=upstream, downstream=downstream).exists():
                            skipped.append(other.name)
                            continue
                        dep = Dependency(
                            upstream=upstream,
                            downstream=downstream,
                            dependency_type=form.cleaned_data['dependency_type'],
                            medium=form.cleaned_data['medium'] or 'unspecified',
                            redundancy_group=form.cleaned_data['redundancy_group'],
                            description=form.cleaned_data['description'],
                        )
                        try:
                            dep.full_clean()
                        except ValidationError as e:
                            errors.append(f'{other}: {"; ".join(e.messages)}')
                            continue
                        dep.save()
                        created += 1
                    if errors:
                        raise ValueError('validation')
            except ValueError:
                for msg in errors:
                    form.add_error(None, msg)
            else:
                if created:
                    side = 'upstream links of' if parents else 'dependencies on'
                    messages.success(request, f'Created {created} {side} {anchor}.')
                if skipped:
                    messages.warning(request, 'Already existed, skipped: ' + ', '.join(skipped))
                return redirect(self._return_url(request) or 'plugins:netbox_dependencies:tree')
        return render(request, self.template_name, self._context(request, form))


class DependencyGraphView(PermissionRequiredMixin, View):
    """Visual dependency graph (Cytoscape.js) with maps per location / region."""

    permission_required = 'netbox_dependencies.view_dependency'
    template_name = 'netbox_dependencies/graph.html'

    @staticmethod
    def _int(request, name):
        value = request.GET.get(name, '')
        return int(value) if value.isdigit() else None

    def get(self, request):
        site_id = self._int(request, 'site')
        region_id = None if site_id else self._int(request, 'region')
        map_id = self._int(request, 'map')
        if map_id:
            site_id = region_id = None
        graph = build_graph(
            site_id=site_id, region_id=region_id, map_id=map_id,
            device_id=self._int(request, 'branch'), focus_id=self._int(request, 'focus'),
        )
        limit = int(get_plugin_config('netbox_dependencies', 'graph_max_nodes'))
        graph['too_large'] = None
        graph['can_edit'] = request.user.has_perm('netbox_dependencies.add_dependency')
        graph['can_delete'] = request.user.has_perm('netbox_dependencies.delete_dependency')
        graph['suppress_url'] = reverse('plugins:netbox_dependencies:suppression_add') if request.user.has_perm(
            'netbox_dependencies.add_devicesuppression') else ''
        graph['resume_url'] = reverse('plugins:netbox_dependencies:suppression_resume', args=[0]) if request.user.has_perm(
            'netbox_dependencies.delete_devicesuppression') else ''
        graph['add_url'] = reverse('plugins:netbox_dependencies:add_multiple') if request.user.has_perm(
            'netbox_dependencies.add_dependency') else ''
        if limit and len(graph['nodes']) > limit and request.GET.get('force') != '1':
            separator = '&' if request.GET else '?'
            graph['too_large'] = {'nodes': len(graph['nodes']), 'limit': limit,
                                  'force_url': f'{request.get_full_path()}{separator}force=1'}
            graph['nodes'], graph['edges'] = [], []   # nothing is drawn until the user picks a smaller map
        # ?focus=<device>&impact=1 (the link in notifications): open with that device's outage already simulated
        graph['auto_simulate'] = bool(graph_focus := self._int(request, 'focus')) and request.GET.get('impact') == '1'
        graph['focus_device'] = graph_focus
        return render(request, self.template_name, {
            'graph': graph,
            'sidebar': build_sidebar(site_id=site_id, region_id=region_id, map_id=map_id),
            'scope': graph['scope'],
            'scope_all': graph['scope'] == 'all',
        })


class GraphLayoutView(PermissionRequiredMixin, View):
    """Saving the manual layout of a map. POST {scope, positions: {nodeKey: [x, y]}};
    DELETE {scope} forgets the saved layout."""

    permission_required = 'netbox_dependencies.change_dependency'

    @staticmethod
    def _body(request):
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    @staticmethod
    def _valid_scope(scope):
        return isinstance(scope, str) and re.fullmatch(r'all|site:\d+|region:\d+|map:\d+', scope) is not None

    def post(self, request):
        body = self._body(request)
        if not body or not self._valid_scope(body.get('scope')) or not isinstance(body.get('positions'), dict):
            return JsonResponse({'detail': 'Invalid payload.'}, status=400)
        scope = body['scope']
        positions = body['positions']
        if len(positions) > 2000:
            return JsonResponse({'detail': 'Too many positions.'}, status=400)
        wanted = {}
        for node, xy in positions.items():
            try:
                if not re.fullmatch(r'[dx]\d+', node):
                    raise ValueError(node)
                wanted[node] = (float(xy[0]), float(xy[1]))
            except (TypeError, ValueError, IndexError, KeyError):
                return JsonResponse({'detail': 'Invalid position.'}, status=400)
        with transaction.atomic():
            for node, (x, y) in wanted.items():
                NodePosition.objects.update_or_create(node=node, scope=scope, defaults={'x': x, 'y': y})
        known = wanted
        return JsonResponse({'saved': len(known)})

    def delete(self, request):
        body = self._body(request)
        if not body or not self._valid_scope(body.get('scope')):
            return JsonResponse({'detail': 'Invalid payload.'}, status=400)
        deleted, _ = NodePosition.objects.filter(scope=body['scope']).delete()
        return JsonResponse({'deleted': deleted})


@register_model_view(CustomMap, 'list', path='', detail=False)
class CustomMapListView(generic.ObjectListView):
    queryset = CustomMap.objects.select_related('owner').annotate(device_count=Count('devices'))
    table = tables.CustomMapTable
    filterset = CustomMapFilterSet
    filterset_form = CustomMapFilterForm


@register_model_view(CustomMap, 'add', detail=False)
@register_model_view(CustomMap, 'edit')
class CustomMapEditView(generic.ObjectEditView):
    queryset = CustomMap.objects.all()
    form = CustomMapForm

    def alter_object(self, obj, request, url_args, url_kwargs):
        if not obj.pk and request.user.is_authenticated:
            obj.owner = request.user
        return obj


@register_model_view(CustomMap, 'delete')
class CustomMapDeleteView(generic.ObjectDeleteView):
    queryset = CustomMap.objects.all()


@register_model_view(CustomMap, 'bulk_delete', path='delete', detail=False)
class CustomMapBulkDeleteView(generic.BulkDeleteView):
    queryset = CustomMap.objects.annotate(device_count=Count('devices'))
    filterset = CustomMapFilterSet
    table = tables.CustomMapTable


class NotificationSettingsView(PermissionRequiredMixin, View):
    """Notifications: destinations (Teams / Slack / webhook), general settings and recent notifications."""

    permission_required = 'netbox_dependencies.view_notificationsettings'
    template_name = 'netbox_dependencies/settings.html'

    @staticmethod
    def _can_change(request):
        return request.user.has_perm('netbox_dependencies.change_notificationsettings')

    def _render(self, request, form):
        seed_from_environment()
        cfg = effective_settings()
        destinations = list(NotificationDestination.objects.all())
        for destination in destinations:
            destination.masked_url = mask_url(destination.webhook_url)
        state = request.GET.get('state', '')
        search = request.GET.get('q', '').strip()
        recent = OutageNotification.objects.select_related('device').prefetch_related('deliveries__destination')
        if state == 'ongoing':
            recent = recent.filter(resolved_at__isnull=True)
        elif state == 'ended':
            recent = recent.filter(resolved_at__isnull=False)
        elif state == 'problems':     # a message that was tried but never delivered
            recent = recent.filter(Exists(NotificationDelivery.objects.filter(
                episode=OuterRef('pk'), sent_at__isnull=True, attempts__gt=0)))
        if search:
            recent = recent.filter(device__name__icontains=search)
        paginator, page = paginate(request, recent)
        return render(request, self.template_name, {
            'form': form,
            'cfg': cfg,
            'destinations': destinations,
            'row': NotificationSettings.objects.select_related('updated_by').first(),
            'recent': page, 'paginator': paginator, 'page': page,
            'state': state, 'search': search,
            'can_change': self._can_change(request),
        })

    def get(self, request):
        cfg = effective_settings()
        return self._render(request, NotificationSettingsForm(initial={
            'enabled': cfg['enabled'], 'delay_seconds': cfg['delay_seconds'],
            'notify_recovery': cfg['notify_recovery'], 'public_url': cfg['public_url'],
        }))

    def post(self, request):
        if not self._can_change(request):
            raise PermissionDenied
        form = NotificationSettingsForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)
        row = NotificationSettings.objects.first() or NotificationSettings()
        row.enabled = form.cleaned_data['enabled']
        row.delay_seconds = form.cleaned_data['delay_seconds']
        row.notify_recovery = form.cleaned_data['notify_recovery']
        row.public_url = form.cleaned_data['public_url']
        row.updated_by = request.user
        row.save()
        messages.success(request, 'Notification settings saved.')
        return redirect('plugins:netbox_dependencies:settings')


class PollerView(PermissionRequiredMixin, View):
    """ICMP pollers: are they alive, what do they ping, and the shared settings."""

    permission_required = 'netbox_dependencies.view_pollersettings'
    template_name = 'netbox_dependencies/poller.html'
    FIELDS = ('enabled', 'interval_seconds', 'ping_count', 'ping_timeout', 'concurrency', 'only_in_graph',
              'fail_threshold', 'stale_after_seconds', 'exclude_tag', 'alert_enabled', 'alert_after_seconds')

    @staticmethod
    def _can_change(request):
        return request.user.has_perm('netbox_dependencies.change_pollersettings')

    def _render(self, request, form):
        cfg = poller_config()
        now = timezone.now()
        beats = {b.name: b for b in PollerHeartbeat.objects.all()}
        rows = []
        for entry in Poller.objects.prefetch_related('sites'):
            rows.append({'name': entry.name, 'entry': entry, 'beat': beats.pop(entry.name, None)})
        for beat in beats.values():       # reporting, but not registered
            rows.append({'name': beat.name, 'entry': None, 'beat': beat})
        rows.sort(key=lambda r: r['name'].lower())
        for row in rows:
            beat = row['beat']
            if beat:
                beat.silent_for = (now - beat.last_seen).total_seconds()
                beat.is_silent = beat.silent_for > cfg['alert_after_seconds']
            row['overrides'] = [label for label, key in (
                ('interval', 'interval_seconds'), ('packets', 'ping_count'), ('timeout', 'ping_timeout'),
                ('parallel', 'concurrency')) if row['entry'] and getattr(row['entry'], key) is not None]
            row['target_count'] = len(poll_targets(cfg, poller=row['name']))
            settings, _entry = poller_settings(row['name'], cfg)
            worst, needed = round_estimate(row['target_count'], settings)
            row['worst_round'] = worst
            row['interval'] = settings['interval_seconds']
            row['too_slow'] = worst > settings['interval_seconds']
            row['needed_concurrency'] = needed
            beat = row['beat']
            row['last_slow'] = bool(beat and beat.cycle_seconds and beat.cycle_seconds > settings['interval_seconds'])
        tagged = Device.objects.filter(tags__slug=cfg['exclude_tag']).count() if cfg['exclude_tag'] else 0
        total = len(poll_targets(cfg))
        presets = [dict(preset, worst=round_estimate(total, preset)[0], detection=preset['fail_threshold'] * preset['interval_seconds'])
                   for preset in PRESETS]
        return render(request, self.template_name, {
            'form': form, 'cfg': cfg, 'rows': rows, 'tagged': tagged, 'presets': presets, 'total_targets': total,
            'row': PollerSettings.objects.select_related('updated_by').first(),
            'can_change': self._can_change(request),
        })

    def get(self, request):
        cfg = poller_config()
        initial = {key: cfg[key] for key in self.FIELDS}
        initial['ping_timeout'] = str(cfg['ping_timeout'])      # a float would be rendered with a decimal comma
        return self._render(request, PollerSettingsForm(initial=initial))

    def post(self, request):
        if not self._can_change(request):
            raise PermissionDenied
        if request.POST.get('forget'):          # drop the heartbeat of a poller that no longer exists
            PollerHeartbeat.objects.filter(pk=request.POST['forget']).delete()
            messages.success(request, 'The poller was forgotten.')
            return redirect('plugins:netbox_dependencies:poller')
        form = PollerSettingsForm(request.POST)
        if not form.is_valid():
            return self._render(request, form)
        row = PollerSettings.objects.first() or PollerSettings()
        for key in self.FIELDS:
            setattr(row, key, form.cleaned_data[key])
        row.updated_by = request.user
        row.save()
        messages.success(request, 'Poller settings saved. The pollers pick them up in their next round.')
        return redirect('plugins:netbox_dependencies:poller')


class PollerEditView(PermissionRequiredMixin, View):
    """Register / edit a poller."""

    permission_required = 'netbox_dependencies.change_pollersettings'
    template_name = 'netbox_dependencies/poller_form.html'

    @staticmethod
    def _instance(pk):
        return get_object_or_404(Poller, pk=pk) if pk else None

    def _form_initial(self, instance, request):
        if not instance:
            return {'name': request.GET.get('name', ''), 'enabled': True}
        initial = {key: getattr(instance, key) for key in (
            'name', 'description', 'enabled', 'interval_seconds', 'ping_count', 'concurrency')}
        initial['ping_timeout'] = None if instance.ping_timeout is None else str(instance.ping_timeout)
        initial['sites'] = list(instance.sites.all())
        return initial

    def get(self, request, pk=None):
        instance = self._instance(pk)
        return render(request, self.template_name, {
            'form': PollerForm(initial=self._form_initial(instance, request), instance=instance), 'instance': instance})

    def post(self, request, pk=None):
        instance = self._instance(pk)
        form = PollerForm(request.POST, instance=instance)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form, 'instance': instance})
        entry = instance or Poller()
        for key in ('name', 'description', 'enabled', 'interval_seconds', 'ping_count', 'ping_timeout', 'concurrency'):
            setattr(entry, key, form.cleaned_data[key])
        entry.save()
        entry.sites.set(form.cleaned_data['sites'])
        messages.success(request, f'Poller "{entry.name}" saved. It picks the change up in its next round.')
        return redirect('plugins:netbox_dependencies:poller')


class PollerDeleteView(PermissionRequiredMixin, View):
    """Unregister a poller (the container is not touched; it keeps working with the shared settings)."""

    permission_required = 'netbox_dependencies.change_pollersettings'

    def post(self, request, pk):
        entry = get_object_or_404(Poller, pk=pk)
        name = entry.name
        entry.delete()
        messages.success(request, f'Poller "{name}" was unregistered.')
        return redirect('plugins:netbox_dependencies:poller')


class DestinationEditView(PermissionRequiredMixin, View):
    """Add / edit a notification destination."""

    permission_required = 'netbox_dependencies.change_notificationsettings'
    template_name = 'netbox_dependencies/destination_form.html'

    @staticmethod
    def _instance(pk):
        return get_object_or_404(NotificationDestination, pk=pk) if pk else None

    def get(self, request, pk=None):
        instance = self._instance(pk)
        initial = ({'name': instance.name, 'kind': instance.kind, 'enabled': instance.enabled}
                   if instance else {'kind': DestinationKindChoices.TEAMS, 'enabled': True})
        form = DestinationForm(initial=initial, instance=instance)
        return render(request, self.template_name, {'form': form, 'instance': instance})

    def post(self, request, pk=None):
        instance = self._instance(pk)
        form = DestinationForm(request.POST, instance=instance)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form, 'instance': instance})
        data = form.cleaned_data
        destination = instance or NotificationDestination()
        destination.name, destination.kind, destination.enabled = data['name'], data['kind'], data['enabled']
        if data['webhook_url']:
            destination.webhook_url = data['webhook_url']
        if data['clear_secret']:
            destination.secret = ''
        elif data['secret']:
            destination.secret = data['secret']
        if destination.kind != DestinationKindChoices.GENERIC:
            destination.secret = ''     # the signature is used for the generic webhook only
        destination.save()
        messages.success(request, f'Destination "{destination.name}" saved.')
        return redirect('plugins:netbox_dependencies:settings')


class DestinationDeleteView(PermissionRequiredMixin, View):
    permission_required = 'netbox_dependencies.change_notificationsettings'

    def post(self, request, pk):
        destination = get_object_or_404(NotificationDestination, pk=pk)
        name = destination.name
        destination.delete()
        messages.success(request, f'Destination "{name}" deleted.')
        return redirect('plugins:netbox_dependencies:settings')


class DestinationTestView(PermissionRequiredMixin, View):
    """Sends two sample messages (outage + recovery) to the destination."""

    permission_required = 'netbox_dependencies.change_notificationsettings'

    def post(self, request, pk):
        destination = get_object_or_404(NotificationDestination, pk=pk)
        try:
            send_samples(destination)
        except DeliveryError as exc:
            messages.error(request, f'The sample messages could not be sent to "{destination.name}" ({exc}).')
        else:
            messages.success(request, f'Two sample messages (outage and recovery) were accepted by "{destination.name}" '
                                      f'(HTTP 2xx). Check the chat or channel.')
        return redirect('plugins:netbox_dependencies:settings')


CYTOSCAPE_PATH = Path(__file__).parent / 'vendor' / 'cytoscape.min.js'


class CytoscapeScriptView(View):
    """Vendored Cytoscape.js library (no CDN, works in an isolated network)."""

    def get(self, request):
        response = FileResponse(open(CYTOSCAPE_PATH, 'rb'), content_type='application/javascript')
        response['Cache-Control'] = 'public, max-age=86400'
        return response
