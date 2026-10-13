from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse

from netbox.models import NetBoxModel

from .choices import DependencyTypeChoices, DestinationKindChoices, MediumChoices, SourceChoices, StateChoices
from .graph import would_create_cycle


class Dependency(NetBoxModel):
    """Device `downstream` depends on device `upstream`
    (if upstream fails, the outage affects downstream)."""

    upstream = models.ForeignKey(
        to='dcim.Device',
        on_delete=models.CASCADE,
        related_name='downstream_dependencies',
        verbose_name='Upstream device',
        help_text='Parent device — its outage affects the downstream device.',
    )
    downstream = models.ForeignKey(
        to='dcim.Device',
        on_delete=models.CASCADE,
        related_name='upstream_dependencies',
        verbose_name='Downstream device',
        help_text='Dependent device.',
    )
    dependency_type = models.CharField(
        max_length=30,
        choices=DependencyTypeChoices,
        default=DependencyTypeChoices.TYPE_NETWORK,
        verbose_name='Type',
    )
    medium = models.CharField(
        max_length=20,
        choices=MediumChoices,
        default=MediumChoices.UNSPECIFIED,
        verbose_name='Medium',
        help_text='Physical medium of the link (drawn differently in the graph).',
    )
    redundancy_group = models.CharField(
        max_length=100,
        blank=True,
        verbose_name='Redundancy group',
        help_text='Dependencies of the same downstream device sharing this name are '
                  'redundant: the device goes down only when ALL of them are down. '
                  'Empty = no redundancy.',
    )
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ('upstream__name', 'downstream__name')
        verbose_name = 'dependency'
        verbose_name_plural = 'dependencies'
        constraints = (
            models.UniqueConstraint(
                fields=('upstream', 'downstream'),
                name='%(app_label)s_%(class)s_unique_pair',
            ),
            models.CheckConstraint(
                condition=~Q(upstream=F('downstream')),
                name='%(app_label)s_%(class)s_no_self_dependency',
            ),
        )

    def __str__(self):
        return f'{self.downstream} ← {self.upstream}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_dependencies:dependency', args=[self.pk])

    def get_dependency_type_color(self):
        return DependencyTypeChoices.colors.get(self.dependency_type)

    def get_medium_color(self):
        return MediumChoices.colors.get(self.medium)

    def clean(self):
        super().clean()
        if self.upstream_id is None or self.downstream_id is None:
            return
        if self.upstream_id == self.downstream_id:
            raise ValidationError('A device cannot depend on itself.')
        edges = Dependency.objects.exclude(pk=self.pk).values_list(
            'upstream_id', 'downstream_id', 'redundancy_group'
        )
        if would_create_cycle(edges, self.upstream_id, self.downstream_id):
            raise ValidationError(
                f'{self.upstream} already depends (directly or indirectly) on '
                f'{self.downstream} — this would create a dependency cycle.'
            )


class DeviceStatus(models.Model):
    """Current availability state of a device (written by the Zabbix sync job or the
    ICMP poller through the API). Deliberately a plain model — high-frequency writes
    must not fill the changelog."""

    device = models.OneToOneField(
        to='dcim.Device',
        on_delete=models.CASCADE,
        related_name='dependency_status',
    )
    state = models.CharField(max_length=20, choices=StateChoices, default=StateChoices.UNKNOWN)
    source = models.CharField(max_length=20, choices=SourceChoices)
    detail = models.CharField(max_length=500, blank=True)
    fail_count = models.PositiveIntegerField(default=0)
    last_checked = models.DateTimeField()
    last_changed = models.DateTimeField()

    class Meta:
        ordering = ('device__name',)
        verbose_name = 'device status'
        verbose_name_plural = 'device statuses'

    def __str__(self):
        return f'{self.device}: {self.state}'

    def get_state_color(self):
        return StateChoices.colors.get(self.state)


class NodePosition(models.Model):
    """Manually saved position of a node on a map (Dude style). `node` is the node key in the graph:
    'd<device id>' or 'x<site id>' (cloud). The position is separate for every map
    (scope: 'all', 'site:<id>', 'region:<id>', 'map:<id>')."""

    node = models.CharField(max_length=20)
    scope = models.CharField(max_length=40)
    x = models.FloatField()
    y = models.FloatField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=('node', 'scope'), name='%(app_label)s_%(class)s_unique'),
        )


class CustomMap(NetBoxModel):
    """Custom (named) map: manually chosen devices + its own layout.
    The layout is stored in NodePosition with scope 'map:<id>'."""

    name = models.CharField(max_length=100)
    description = models.CharField(max_length=200, blank=True)
    owner = models.ForeignKey(
        to='users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Who created the map (informational — maps are visible to everyone who may view them).',
    )
    devices = models.ManyToManyField(to='dcim.Device', related_name='custom_maps', blank=True)

    class Meta:
        ordering = ('name',)
        verbose_name = 'custom map'
        verbose_name_plural = 'custom maps'
        constraints = (
            models.UniqueConstraint(fields=('name',), name='%(app_label)s_%(class)s_unique_name'),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return f"{reverse('plugins:netbox_dependencies:graph')}?map={self.pk}"


@receiver(post_delete, sender=CustomMap)
def _delete_custom_map_layout(sender, instance, **kwargs):
    NodePosition.objects.filter(scope=f'map:{instance.pk}').delete()


class OutageNotification(models.Model):
    """An outage episode of a root cause (device, since when, until when, maximum impact). What was sent
    where is recorded in NotificationDelivery."""

    device = models.ForeignKey(to='dcim.Device', on_delete=models.CASCADE, related_name='+')
    started_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)
    peak_down = models.PositiveIntegerField(default=0)
    peak_degraded = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ('-started_at',)
        constraints = (
            models.UniqueConstraint(
                fields=('device',), condition=Q(resolved_at__isnull=True),
                name='%(app_label)s_%(class)s_one_open_per_device'),
        )

    def __str__(self):
        return f'{self.device} since {self.started_at:%Y-%m-%d %H:%M}'


class NotificationDestination(models.Model):
    """Notification destination: Teams / Slack / generic webhook. The webhook URL and the signing key are secrets —
    deliberately a plain model (not a NetBoxModel) so they never end up in the changelog."""

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=DestinationKindChoices, default=DestinationKindChoices.TEAMS)
    webhook_url = models.CharField(max_length=2048)
    secret = models.CharField(max_length=200, blank=True,
                              help_text='Generic webhook only: requests are signed with HMAC-SHA256 using this key.')
    enabled = models.BooleanField(default=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_ok = models.BooleanField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ('name',)

    def __str__(self):
        return self.name


class NotificationDelivery(models.Model):
    """Delivery of one message (outage / recovery) to one destination — so when one destination fails,
    only that destination is retried."""

    KIND_OUTAGE = 'outage'
    KIND_RECOVERY = 'recovery'

    episode = models.ForeignKey(to=OutageNotification, on_delete=models.CASCADE, related_name='deliveries')
    destination = models.ForeignKey(to=NotificationDestination, on_delete=models.CASCADE, related_name='deliveries')
    kind = models.CharField(max_length=10)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=('episode', 'destination', 'kind'), name='%(app_label)s_%(class)s_unique'),
        )

    def __str__(self):
        return f'{self.kind} -> {self.destination}'


class NotificationSettings(models.Model):
    """General notification settings editable in the UI (singleton). Values from the UI take precedence over
    PLUGINS_CONFIG (env); an empty public URL = use the value from env. Destinations are in NotificationDestination."""

    enabled = models.BooleanField(default=True)
    delay_seconds = models.PositiveIntegerField(default=120)
    notify_recovery = models.BooleanField(default=True)
    public_url = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        to='users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    class Meta:
        verbose_name = 'notification settings'
        verbose_name_plural = 'notification settings'

    def save(self, *args, **kwargs):
        self.pk = 1   # singleton
        super().save(*args, **kwargs)
