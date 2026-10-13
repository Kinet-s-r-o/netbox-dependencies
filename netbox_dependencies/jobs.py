from netbox.jobs import JobRunner, system_job
from netbox.plugins import get_plugin_config

from .status import sync_from_zabbix
from .notifications import purge_old_notifications, run_notifications
from .watchdog import heal_zabbix_sync

SYNC_JOB_TIMEOUT = 120   # sending notifications can take tens of seconds during a network outage


@system_job(interval=int(get_plugin_config('netbox_dependencies', 'zabbix_sync_interval')))
class ZabbixStatusSyncJob(JobRunner):
    """Periodically overwrites the state of devices from the local snapshot of the
    netbox_zabbix_status plugin (no Zabbix API call)."""

    class Meta:
        name = 'Dependencies: device status from Zabbix'

    @classmethod
    def enqueue(cls, *args, **kwargs):
        kwargs.setdefault('job_timeout', SYNC_JOB_TIMEOUT)
        return super().enqueue(*args, **kwargs)

    def run(self, *args, **kwargs):
        self.job.data = sync_from_zabbix()
        # watchdog: a stuck Zabbix sync is marked as failed and scheduled again
        watchdog = heal_zabbix_sync()
        if watchdog and (watchdog['failed_stale'] or watchdog['rescheduled']):
            self.job.data['watchdog'] = watchdog
            self.logger.warning('Zabbix sync watchdog: %s', watchdog)
        # notifications (new outage / outage ended)
        teams = run_notifications()
        if teams.get('sent') or teams.get('resolved') or teams.get('errors'):
            self.job.data['teams'] = teams
        purged = purge_old_notifications()      # the notification records do not grow forever
        if purged:
            self.job.data['purged_notifications'] = purged
