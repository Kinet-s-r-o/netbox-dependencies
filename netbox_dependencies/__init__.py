from netbox.plugins import PluginConfig

__version__ = '1.18.0'


class DependenciesConfig(PluginConfig):
    name = 'netbox_dependencies'
    verbose_name = 'Dependencies'
    description = 'Device dependency tree and outage impact analysis'
    version = __version__
    author = 'Adrián Javorček'
    author_email = 'adrian.javorcek@gmail.com'
    base_url = 'dependencies'
    min_version = '4.6.0'
    default_settings = {
        # ICMP: number of consecutive failures before a device is marked DOWN
        'fail_threshold': 3,
        # A status from the poller older than N seconds is treated as unknown
        'status_stale_after': 600,
        # Zabbix: a host snapshot older than N seconds is treated as unknown
        'zabbix_stale_after': 900,
        # A Zabbix problem whose name matches any of these regexes (case-insensitive)
        # means that the device is unavailable (DOWN)
        'zabbix_down_patterns': [
            r'unavailable by icmp',
            r'icmp ping',
            r'unreachable',
            r'is down',
            r'no snmp data',
        ],
        # How often (minutes) the status is overwritten from Zabbix
        'zabbix_sync_interval': 1,
        # A graph with more nodes than this is not drawn (pick a location / map); 0 = no limit
        'graph_max_nodes': 1500,
        # Geographic map (Dependencies -> Geographic map): the background tiles. Empty URL = no background (only the sites and links).
        # The default is the public OpenStreetMap server (fair use only); point it to your own tile server for heavy use or an isolated network.
        'map_tile_url': 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
        'map_attribution': '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        'map_max_zoom': 19,
        # None = the street layer above plus the built-in Terrain and Satellite layers; or your own list of
        # {'name': ..., 'url': ..., 'attribution': ..., 'max_zoom': ..., 'dark': False}; [] = no background
        'map_layers': None,
        # Shown when no site has coordinates yet
        'map_default_center': [48.7, 19.5],
        'map_default_zoom': 7,
        # Records of sent notifications are deleted this many days after the outage ended; 0 = never
        'notification_retention_days': 90,
        # Notifications (Teams / Slack / generic webhook); destinations are set up in the UI (Configuration -> Notifications)
        'notifications_enabled': True,
        # A root-cause outage is announced only after it has lasted at least this long (seconds)
        'notification_delay_seconds': 120,
        # Also send a message when the outage ends
        'notify_recovery': True,
        # NetBox address used for links in the messages, e.g. https://netbox.example.com (empty = no links)
        'public_url': '',
        # Legacy: a webhook from env (DEPENDENCIES_TEAMS_WEBHOOK_URL) is taken over once as the destination "Teams"
        'teams_webhook_url': '',
    }

    def ready(self):
        super().ready()
        # An explicit import registers the system job
        from . import jobs  # noqa: F401


config = DependenciesConfig
