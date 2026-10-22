from django import forms
from django.utils import timezone

from dcim.models import Device, Site
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm, NetBoxModelImportForm
from utilities.forms.fields import (
    CSVChoiceField,
    CSVModelChoiceField,
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
)
from utilities.forms.widgets import DateTimePicker

from .choices import DependencyTypeChoices, DestinationKindChoices, MediumChoices
from .models import CustomMap, Dependency, NotificationDestination, PeerLink, Poller


class DependencyForm(NetBoxModelForm):
    upstream = DynamicModelChoiceField(queryset=Device.objects.all(), label='Upstream device')
    downstream = DynamicModelChoiceField(queryset=Device.objects.all(), label='Downstream device')

    class Meta:
        model = Dependency
        fields = (
            'upstream', 'downstream', 'dependency_type', 'medium', 'redundancy_group',
            'description', 'tags',
        )


class DependencyImportForm(NetBoxModelImportForm):
    upstream = CSVModelChoiceField(queryset=Device.objects.all(), to_field_name='name')
    downstream = CSVModelChoiceField(queryset=Device.objects.all(), to_field_name='name')
    dependency_type = CSVChoiceField(choices=DependencyTypeChoices, required=False)
    medium = CSVChoiceField(choices=MediumChoices, required=False)

    class Meta:
        model = Dependency
        fields = (
            'upstream', 'downstream', 'dependency_type', 'medium', 'redundancy_group',
            'description', 'tags',
        )


class PeerLinkForm(NetBoxModelForm):
    device_a = DynamicModelChoiceField(queryset=Device.objects.all(), label='Device A')
    device_b = DynamicModelChoiceField(queryset=Device.objects.all(), label='Device B')

    class Meta:
        model = PeerLink
        fields = ('device_a', 'device_b', 'medium', 'description', 'tags')


class PeerLinkImportForm(NetBoxModelImportForm):
    device_a = CSVModelChoiceField(queryset=Device.objects.all(), to_field_name='name')
    device_b = CSVModelChoiceField(queryset=Device.objects.all(), to_field_name='name')
    medium = CSVChoiceField(choices=MediumChoices, required=False)

    class Meta:
        model = PeerLink
        fields = ('device_a', 'device_b', 'medium', 'description', 'tags')


class PeerLinkFilterForm(NetBoxModelFilterSetForm):
    model = PeerLink
    medium = forms.MultipleChoiceField(choices=MediumChoices, required=False, label='Medium')
    device_a_id = DynamicModelMultipleChoiceField(queryset=Device.objects.all(), required=False, label='Device A')
    device_b_id = DynamicModelMultipleChoiceField(queryset=Device.objects.all(), required=False, label='Device B')


class DependencyFilterForm(NetBoxModelFilterSetForm):
    model = Dependency
    dependency_type = forms.MultipleChoiceField(
        choices=DependencyTypeChoices, required=False, label='Type'
    )
    medium = forms.MultipleChoiceField(choices=MediumChoices, required=False, label='Medium')
    upstream_id = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label='Upstream device'
    )
    downstream_id = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label='Downstream device'
    )
    redundancy_group = forms.CharField(required=False)


class ImpactForm(forms.Form):
    """Selection of devices for the outage simulation."""

    devices = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(),
        label='Failed devices',
        help_text='Select one or more devices to simulate their outage.',
    )


class DependencyMultiAddForm(forms.Form):
    """One upstream + several downstream devices at once."""

    upstream = DynamicModelChoiceField(queryset=Device.objects.all(), label='Upstream device')
    downstreams = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), label='Downstream devices',
        help_text='All selected devices depend on the upstream device.',
    )
    dependency_type = forms.ChoiceField(
        choices=DependencyTypeChoices, initial=DependencyTypeChoices.TYPE_NETWORK, label='Type'
    )
    medium = forms.ChoiceField(
        choices=MediumChoices, initial=MediumChoices.UNSPECIFIED, required=False, label='Medium',
        help_text='Physical medium of the created links. It can be changed later by right-clicking a link in the graph.',
    )
    redundancy_group = forms.CharField(
        max_length=100, required=False, label='Redundancy group',
        help_text='Applied to every created dependency. Empty = no redundancy.',
    )
    description = forms.CharField(max_length=200, required=False)

    def clean(self):
        cleaned = super().clean()
        upstream = cleaned.get('upstream')
        downstreams = cleaned.get('downstreams')
        if not upstream or not downstreams:
            return cleaned
        if upstream in downstreams:
            raise forms.ValidationError('The upstream device cannot also be one of the downstream devices.')
        return cleaned



class DependencyAddParentsForm(DependencyMultiAddForm):
    """One downstream device + several upstream devices (the reverse of the form above)."""

    upstream = None
    downstream = DynamicModelChoiceField(queryset=Device.objects.all(), label='Downstream device')
    upstreams = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), label='Upstream devices',
        help_text='The downstream device depends on every selected device.',
    )
    downstreams = None

    field_order = ('downstream', 'upstreams', 'dependency_type', 'medium', 'redundancy_group', 'description')

    def clean(self):
        cleaned = forms.Form.clean(self)
        downstream, upstreams = cleaned.get('downstream'), cleaned.get('upstreams')
        if downstream and upstreams and downstream in upstreams:
            raise forms.ValidationError('The downstream device cannot also be one of the upstream devices.')
        return cleaned


class DeviceSuppressionForm(forms.Form):
    """Take a device out of the outage analysis for a while."""

    device = DynamicModelChoiceField(queryset=Device.objects.all(), label='Device')
    reason = forms.CharField(max_length=200, required=False, label='Reason',
                             help_text='For example "backup unit, switched off until the new line is installed".')
    until = forms.DateTimeField(
        required=False, label='Until', widget=DateTimePicker(),
        help_text='The suppression ends by itself at this time. Empty = until somebody resumes the device.')
    include_dependents = forms.BooleanField(
        required=False, label='Also everything below it',
        help_text='Ignore the devices that depend on this one as well (they are expected to be off too).')

    def clean_until(self):
        until = self.cleaned_data['until']
        if until and until <= timezone.now():
            raise forms.ValidationError('The end must be in the future.')
        return until


class CustomMapForm(NetBoxModelForm):
    devices = DynamicModelMultipleChoiceField(
        queryset=Device.objects.all(), required=False, label='Devices',
        help_text='Devices shown on this map (their dependencies are drawn automatically).',
    )

    class Meta:
        model = CustomMap
        fields = ('name', 'description', 'devices', 'tags')


class CustomMapFilterForm(NetBoxModelFilterSetForm):
    model = CustomMap


class NotificationSettingsForm(forms.Form):
    """General notification settings (destinations are managed separately)."""

    enabled = forms.BooleanField(required=False, label='Send notifications')
    delay_seconds = forms.IntegerField(
        min_value=0, max_value=86400, label='Delay before the first message (seconds)',
        help_text='A root-cause outage must last at least this long before it is announced. '
                  'Short outages that end sooner produce no message.',
    )
    notify_recovery = forms.BooleanField(
        required=False, label='Also send a message when the outage ends')
    public_url = forms.URLField(
        required=False, label='NetBox address used in links',
        help_text='For example https://netbox.example.com (no trailing slash). Empty = use the environment value; '
                  'if that is empty too, messages have no buttons.',
    )

    def clean_public_url(self):
        return self.cleaned_data['public_url'].rstrip('/')


class PollerSettingsForm(forms.Form):
    """Settings of the ICMP poller; the poller downloads them every cycle (no restart needed)."""

    enabled = forms.BooleanField(
        required=False, label='Poller enabled',
        help_text='Off: the poller keeps reporting that it is alive but pings nothing (statuses age out to unknown).')
    interval_seconds = forms.IntegerField(
        min_value=10, max_value=3600, label='Interval between rounds (seconds)',
        help_text='How often all devices are pinged. A round must finish within the interval, otherwise rounds follow back to back.')
    ping_count = forms.IntegerField(min_value=1, max_value=10, label='Packets per device')
    ping_timeout = forms.FloatField(
        min_value=0.2, max_value=10, label='Timeout per packet (seconds)',
        widget=forms.NumberInput(attrs={'step': '0.1'}))
    concurrency = forms.IntegerField(
        min_value=1, max_value=5000, label='Devices pinged at once',
        help_text='Higher is faster but opens more raw sockets at the same time.')
    only_in_graph = forms.BooleanField(
        required=False, label='Only devices that have a dependency',
        help_text='Off: every active device with a primary IP (without a Zabbix host) is pinged, also standalone ones.')
    fail_threshold = forms.IntegerField(
        min_value=1, max_value=20, label='Failed rounds before a device is DOWN',
        help_text='Protection against flapping: a single lost round does not raise an outage.')
    stale_after_seconds = forms.IntegerField(
        min_value=60, max_value=86400, label='A status older than this is "unknown" (seconds)',
        help_text='Should be a few times the interval, otherwise a slow round turns statuses to unknown.')
    exclude_tag = forms.SlugField(
        required=False, max_length=100, label='Exclusion tag',
        help_text='Devices carrying this NetBox tag are never pinged (for example "no-ping"). Empty = no exclusion.')
    alert_enabled = forms.BooleanField(
        required=False, label='Notify when the poller goes silent',
        help_text='Sent to the notification destinations (Dependencies → Notifications).')
    alert_after_seconds = forms.IntegerField(
        min_value=60, max_value=86400, label='Silent for this long means an alert (seconds)',
        help_text='Should be longer than the interval plus the time one round takes.')

    def clean(self):
        cleaned = super().clean()
        stale, interval = cleaned.get('stale_after_seconds'), cleaned.get('interval_seconds')
        if stale and interval and stale <= interval:
            self.add_error('stale_after_seconds', 'Must be longer than the interval, otherwise statuses expire between rounds.')
        return cleaned


class PollerForm(forms.Form):
    """Register / edit one ICMP poller: description, scope and its own settings."""

    name = forms.CharField(
        max_length=100, label='Name',
        help_text='Exactly the POLLER_NAME of the poller container (or its host name when POLLER_NAME is not set).')
    description = forms.CharField(max_length=200, required=False, label='Description',
                                  help_text='For example "Branch office network".')
    enabled = forms.BooleanField(required=False, initial=True, label='Enabled',
                                 help_text='Off: the poller keeps reporting but pings nothing.')
    sites = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False, label='Sites',
        help_text='The poller pings only devices of these sites. Empty = every device that no other poller claims.')
    interval_seconds = forms.IntegerField(min_value=10, max_value=3600, required=False, label='Interval (seconds)')
    ping_count = forms.IntegerField(min_value=1, max_value=10, required=False, label='Packets per device')
    ping_timeout = forms.FloatField(min_value=0.2, max_value=10, required=False, label='Timeout per packet (seconds)',
                                    widget=forms.NumberInput(attrs={'step': '0.1'}))
    concurrency = forms.IntegerField(min_value=1, max_value=5000, required=False, label='Devices pinged at once')

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        for field in ('interval_seconds', 'ping_count', 'ping_timeout', 'concurrency'):
            self.fields[field].help_text = self.fields[field].help_text or 'Empty = the shared value.'

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        clash = Poller.objects.filter(name__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('A poller with this name is registered already.')
        return name


class DestinationForm(forms.Form):
    """Notification destination: Teams / Slack / generic webhook."""

    name = forms.CharField(max_length=100, label='Name', help_text='Any label, for example "Teams – NOC".')
    kind = forms.ChoiceField(choices=DestinationKindChoices, label='Type')
    webhook_url = forms.CharField(
        required=False, label='Webhook URL', max_length=2048,
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'spellcheck': 'false'}),
        help_text='Stored but never shown again (it usually contains a signature). When editing, leave empty to keep the saved URL.',
    )
    secret = forms.CharField(
        required=False, max_length=200, label='Signing secret (generic webhook only)',
        widget=forms.TextInput(attrs={'autocomplete': 'off', 'spellcheck': 'false'}),
        help_text='If set, every request carries the header X-Signature-256: sha256=<HMAC-SHA256 of the body>. '
                  'When editing, leave empty to keep the saved secret.',
    )
    clear_secret = forms.BooleanField(required=False, label='Remove the saved signing secret')
    enabled = forms.BooleanField(required=False, initial=True, label='Enabled')

    def __init__(self, *args, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = instance

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        clash = NotificationDestination.objects.filter(name__iexact=name)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError('A destination with this name already exists.')
        return name

    def clean_webhook_url(self):
        url = self.cleaned_data['webhook_url'].strip()
        if not url and (self.instance is None or not self.instance.webhook_url):
            raise forms.ValidationError('The webhook URL is required.')
        if url:
            from django.core.validators import URLValidator
            URLValidator(schemes=['http', 'https'])(url)
        return url
