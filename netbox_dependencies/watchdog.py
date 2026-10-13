"""Watchdog of the Zabbix sync.

NetBox does not schedule the next run of a periodic job while a job in the state pending / scheduled /
running exists. If a worker stops while the sync runs (container restart), the record stays
`running` forever and the sync never starts again.
This watchdog runs in the minute job of the plugin: it marks the stuck record as failed
and, if no further run is scheduled, schedules the sync again."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .status import zabbix_available

# The Zabbix sync has a 2 min timeout; anything running longer than 15 min is certainly stuck.
STALE_AFTER = timedelta(minutes=15)
# Shortly after a run finishes the next one is scheduled by the job itself (finally block) — do not interfere then.
GRACE = timedelta(seconds=90)


def heal_zabbix_sync():
    """Returns {'failed_stale': [pk, ...], 'rescheduled': bool} or None if the plugin is not installed."""
    if not zabbix_available():
        return None
    from core.choices import JobStatusChoices
    from netbox_zabbix_status.jobs import ZabbixSyncJob
    from netbox_zabbix_status.zabbix import get_setting

    now = timezone.now()
    jobs = ZabbixSyncJob.get_jobs()

    stale = jobs.filter(status=JobStatusChoices.STATUS_RUNNING).filter(
        Q(started__lt=now - STALE_AFTER) | Q(started__isnull=True, created__lt=now - STALE_AFTER)
    )
    # "just finished" is judged before we intervene: marking a stuck job is not a finished sync
    just_finished = jobs.filter(completed__gte=now - GRACE).exists()
    failed = []
    for job in stale:
        job.terminate(
            status=JobStatusChoices.STATUS_FAILED,
            error='Stale job: it stayed in the "running" state (the worker was probably stopped). '
                  'Marked as failed by the netbox_dependencies watchdog.',
        )
        failed.append(job.pk)

    rescheduled = False
    nothing_enqueued = not jobs.filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()
    if nothing_enqueued and not just_finished:
        ZabbixSyncJob.enqueue_once(interval=int(get_setting('sync_interval', 5)))
        rescheduled = True
    return {'failed_stale': failed, 'rescheduled': rescheduled}
