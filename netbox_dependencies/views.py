import json
import re
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View

from dcim.models import Device
from netbox.plugins import get_plugin_config
from netbox.views import generic
from utilities.paginator import get_paginate_count
from utilities.views import ViewTab, register_model_view

from . import tables
from .choices import DestinationKindChoices
from .filtersets import CustomMapFilterSet, DependencyFilterSet
from .forms import (
    CustomMapFilterForm,
    CustomMapForm,
    DependencyFilterForm,
    DependencyForm,
    DependencyImportForm,
    DependencyMultiAddForm,
    DestinationForm,
    ImpactForm,
    NotificationSettingsForm,
)
from .graph_data import build_graph, build_sidebar
from .impact import get_impact
from .models import (
    CustomMap,
    Dependency,
    NodePosition,
    NotificationDestination,
    NotificationSettings,
    OutageNotification,
)
from .notifications import DeliveryError, effective_settings, mask_url, seed_from_environment, send_samples
from .pagination import paginate
from .status import analyze_outages
from .tree import build_tree


@register_model_view(Dependency, 'list', path='', detail=False)
class DependencyListView(generic.ObjectListView):
    queryset = Dependency.objects.select_related('upstream', 'downstream')
    table = tables.DependencyTable
    filterset = DependencyFilterSet
    filterset_form = DependencyFilterForm


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
        return render(request, self.template_name, {
            'outages': outages[:MAX_ROOTS_SHOWN], 'outages_total': len(outages), 'max_roots': MAX_ROOTS_SHOWN,
            'explained': page.object_list, 'explained_total': len(explained), 'page': page, 'paginator': paginator,
        })


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
    """Creates one dependency for each selected downstream device."""

    permission_required = 'netbox_dependencies.add_dependency'
    template_name = 'netbox_dependencies/add_multiple.html'

    def get(self, request):
        initial = {}
        if request.GET.get('upstream'):
            initial['upstream'] = request.GET['upstream']
        return render(request, self.template_name, {'form': DependencyMultiAddForm(initial=initial)})

    def post(self, request):
        form = DependencyMultiAddForm(request.POST)
        if form.is_valid():
            upstream = form.cleaned_data['upstream']
            created, skipped, errors = 0, [], []
            try:
                with transaction.atomic():
                    for device in form.cleaned_data['downstreams']:
                        if Dependency.objects.filter(upstream=upstream, downstream=device).exists():
                            skipped.append(device.name)
                            continue
                        dep = Dependency(
                            upstream=upstream,
                            downstream=device,
                            dependency_type=form.cleaned_data['dependency_type'],
                            medium=form.cleaned_data['medium'] or 'unspecified',
                            redundancy_group=form.cleaned_data['redundancy_group'],
                            description=form.cleaned_data['description'],
                        )
                        try:
                            dep.full_clean()
                        except ValidationError as e:
                            errors.append(f'{device}: {"; ".join(e.messages)}')
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
                    messages.success(request, f'Created {created} dependencies on {upstream}.')
                if skipped:
                    messages.warning(request, 'Already existed, skipped: ' + ', '.join(skipped))
                return redirect('plugins:netbox_dependencies:tree')
        return render(request, self.template_name, {'form': form})


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
        if limit and len(graph['nodes']) > limit and request.GET.get('force') != '1':
            separator = '&' if request.GET else '?'
            graph['too_large'] = {'nodes': len(graph['nodes']), 'limit': limit,
                                  'force_url': f'{request.get_full_path()}{separator}force=1'}
            graph['nodes'], graph['edges'] = [], []   # nothing is drawn until the user picks a smaller map
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
        return render(request, self.template_name, {
            'form': form,
            'cfg': cfg,
            'destinations': destinations,
            'row': NotificationSettings.objects.select_related('updated_by').first(),
            'recent': OutageNotification.objects.select_related('device').prefetch_related('deliveries__destination')[:15],
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
