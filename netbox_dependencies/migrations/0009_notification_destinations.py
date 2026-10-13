import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def move_to_destinations(apps, schema_editor):
    """A webhook from the older setting (or from env) becomes the destination "Teams" and the notifications that were
    already sent are recorded as delivered, so nothing is sent a second time after the migration."""
    Settings = apps.get_model('netbox_dependencies', 'NotificationSettings')
    Destination = apps.get_model('netbox_dependencies', 'NotificationDestination')
    Episode = apps.get_model('netbox_dependencies', 'OutageNotification')
    Delivery = apps.get_model('netbox_dependencies', 'NotificationDelivery')

    row = Settings.objects.first()
    env_url = ((getattr(settings, 'PLUGINS_CONFIG', None) or {}).get('netbox_dependencies') or {}).get(
        'teams_webhook_url', '')
    url = (row.teams_webhook_url if row else '') or env_url
    if not url or (row is None and not Episode.objects.exists()):
        return   # nothing to carry over (a webhook from env is taken over on the first run)
    destination = Destination.objects.create(name='Teams', kind='teams', webhook_url=url, enabled=True)
    if row is None:
        Settings.objects.update_or_create(pk=1, defaults={})   # a settings row = "env has already been taken over"
    for episode in Episode.objects.all():
        if episode.notified_at:
            Delivery.objects.create(
                episode=episode, destination=destination, kind='outage',
                sent_at=episode.notified_at, attempts=max(episode.attempts, 1))
        if episode.recovery_notified_at:
            Delivery.objects.create(
                episode=episode, destination=destination, kind='recovery',
                sent_at=episode.recovery_notified_at, attempts=max(episode.recovery_attempts, 1))


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_dependencies', '0008_notificationsettings'),
    ]

    operations = [
        migrations.CreateModel(
            name='NotificationDestination',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('kind', models.CharField(default='teams', max_length=20)),
                ('webhook_url', models.CharField(max_length=2048)),
                ('secret', models.CharField(blank=True, max_length=200)),
                ('enabled', models.BooleanField(default=True)),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('last_ok', models.BooleanField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=300)),
            ],
            options={
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='NotificationDelivery',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('kind', models.CharField(max_length=10)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('last_error', models.CharField(blank=True, max_length=300)),
                ('episode', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='netbox_dependencies.outagenotification')),
                ('destination', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='netbox_dependencies.notificationdestination')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('episode', 'destination', 'kind'), name='netbox_dependencies_notificationdelivery_unique')],
            },
        ),
        # the data is moved while the old columns still exist
        migrations.RunPython(move_to_destinations, migrations.RunPython.noop),
        # settings: rename instead of losing the values
        migrations.RenameField(model_name='notificationsettings', old_name='teams_enabled', new_name='enabled'),
        migrations.RenameField(model_name='notificationsettings', old_name='teams_delay_seconds', new_name='delay_seconds'),
        migrations.RenameField(model_name='notificationsettings', old_name='teams_notify_recovery', new_name='notify_recovery'),
        migrations.RemoveField(model_name='notificationsettings', name='teams_webhook_url'),
        # the delivery state is now per destination (NotificationDelivery)
        migrations.RemoveField(model_name='outagenotification', name='attempts'),
        migrations.RemoveField(model_name='outagenotification', name='last_error'),
        migrations.RemoveField(model_name='outagenotification', name='notified_at'),
        migrations.RemoveField(model_name='outagenotification', name='recovery_attempts'),
        migrations.RemoveField(model_name='outagenotification', name='recovery_notified_at'),
    ]
