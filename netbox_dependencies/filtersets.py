import django_filters
from django.db.models import Q

from dcim.models import Device
from netbox.filtersets import NetBoxModelFilterSet

from .choices import DependencyTypeChoices, MediumChoices
from .models import CustomMap, Dependency


class DependencyFilterSet(NetBoxModelFilterSet):
    dependency_type = django_filters.MultipleChoiceFilter(choices=DependencyTypeChoices)
    medium = django_filters.MultipleChoiceFilter(choices=MediumChoices)
    upstream_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label='Upstream device (ID)'
    )
    downstream_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), label='Downstream device (ID)'
    )
    device_id = django_filters.ModelMultipleChoiceFilter(
        queryset=Device.objects.all(), method='filter_device', label='Device on either side (ID)'
    )

    class Meta:
        model = Dependency
        fields = ('id', 'dependency_type', 'medium', 'redundancy_group', 'description')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(
            Q(upstream__name__icontains=value)
            | Q(downstream__name__icontains=value)
            | Q(redundancy_group__icontains=value)
            | Q(description__icontains=value)
        )

    def filter_device(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(Q(upstream__in=value) | Q(downstream__in=value))


class CustomMapFilterSet(NetBoxModelFilterSet):
    class Meta:
        model = CustomMap
        fields = ('id', 'name', 'description')

    def search(self, queryset, name, value):
        if not value.strip():
            return queryset
        return queryset.filter(Q(name__icontains=value) | Q(description__icontains=value))
