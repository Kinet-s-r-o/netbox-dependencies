# NetBox Dependencies

A [NetBox](https://github.com/netbox-community/netbox) plugin that models **which device depends on which**
and answers the question *"if this device fails, what else goes down?"* — on a graph, in a tree, and
live, with optional notifications to Microsoft Teams, Slack or any webhook.

![Dependency graph with a live outage](docs/images/graph.png)

<details><summary>Dark theme</summary>

![Dependency graph, dark theme](docs/images/graph-dark.png)

</details>

*Screenshots use fictional demo data.*

## Features

- **Dependencies** between devices: upstream → downstream, link medium (Ethernet, optical, radio, VPN / tunnel, LTE / 5G, satellite), optional
  **redundancy groups** (a device goes *down* only when all its redundant parents are down, otherwise it is *degraded*).
  Cycles, self-references and duplicates are rejected. CSV import and REST API included.
- **Interactive graph** (Cytoscape.js) per site, region or custom map; other locations appear as clouds that open
  their own map. Search, filters, role icons, PNG export, minimap. Click a link for its details (medium, type, redundancy group, description). Layouts are saved automatically.
- **Outage simulation**: right-click a device in the graph (or use the simulation page) to see everything it would take down.
- **Live status** from two sources: the [Zabbix](#zabbix-optional) sync and a built-in **ICMP poller** for devices
  that are not monitored elsewhere.
- **Root-cause analysis**: a live outage is split into *root causes* and devices that are merely *explained* by an upstream failure.
- **Notifications** to any number of destinations (Teams, Slack, generic JSON webhook): one message per root-cause outage,
  one when it ends.
- Tree view, device tab "Dependencies", pagination and limits for large networks.

![Outage simulation](docs/images/impact.png)

## Compatibility

Developed and tested with **NetBox v4.6.x** (`min_version = 4.6.0`), Python 3.12+.
Other NetBox versions are not tested.

## Installation

The plugin has no dependencies besides NetBox itself.

```bash
# inside the NetBox virtualenv
pip install git+https://github.com/adrian-13/netbox-dependencies.git@v1.4.0
```

`configuration.py` (or `configuration/plugins.py` in netbox-docker):

```python
PLUGINS = ['netbox_dependencies']

PLUGINS_CONFIG = {
    'netbox_dependencies': {
        # all optional — see "Configuration" below
        'public_url': 'https://netbox.example.com',
    },
}
```

Then apply the migrations and restart NetBox and its worker (the worker runs the status job):

```bash
python manage.py migrate
```

### netbox-docker

Build a custom image with the plugin — see [examples/Dockerfile-Plugins](examples/Dockerfile-Plugins) and the
[netbox-docker wiki](https://github.com/netbox-community/netbox-docker/wiki/Using-Netbox-Plugins), add the two
`PLUGINS` / `PLUGINS_CONFIG` lines to `configuration/plugins.py`, then `docker compose build && docker compose up -d`.
Migrations run automatically on container start.

## Configuration

All keys of `PLUGINS_CONFIG['netbox_dependencies']` are optional:

| Key | Default | Meaning |
|---|---|---|
| `fail_threshold` | `3` | default for "failed rounds before DOWN" (change it later on the ICMP poller page) |
| `status_stale_after` | `600` | default for "a status older than this is unknown" (also editable on the poller page) |
| `zabbix_stale_after` | `900` | the same for the Zabbix snapshot |
| `zabbix_down_patterns` | see `__init__.py` | regexes; a Zabbix problem whose name matches means "unavailable" |
| `zabbix_sync_interval` | `1` | minutes between status syncs |
| `graph_max_nodes` | `1500` | larger graphs are not drawn (pick a site / map instead); `0` = no limit |
| `notifications_enabled`, `notification_delay_seconds`, `notify_recovery`, `public_url` | `True`, `120`, `True`, `''` | initial defaults for notifications; they can be changed in the UI afterwards |
| `notification_retention_days` | `90` | delete notification records this many days after the outage ended; `0` = keep |

## ICMP poller

Devices that are not monitored in Zabbix can be pinged by a small separate container. At the start of every round it downloads the
list of targets **and its settings** from NetBox, pings the targets in parallel and reports raw results plus a heartbeat; the decision "DOWN"
(failure threshold) is made by the plugin.

1. Create a NetBox user for the poller and an API token. Give it exactly these permissions:
   *view device*, *view dependency*, *change device status* (Dependencies → device status).
2. Run the container — see [examples/docker-compose.poller.yml](examples/docker-compose.poller.yml). It needs
   `cap_add: NET_RAW` for raw ICMP sockets.

Container environment variables: `NETBOX_URL`, `NETBOX_TOKEN` (v1 and v2 tokens both work), `POLLER_NAME` (shown in NetBox; default the
container's host name) and `VERIFY_SSL` (1). `POLL_INTERVAL` (60), `PING_COUNT` (3), `PING_TIMEOUT` (1) and `PING_CONCURRENCY` (500) are only
fallbacks until NetBox answers.

![ICMP poller page](docs/images/poller.png)

Everything else is managed in **Dependencies → Configuration → ICMP poller**, and a change applies in the poller's next round (no restart):

- **Is it alive?** One row per poller: last report, how long the last round took, how many devices it pinged and how many are down.
- **Settings:** enabled / paused, interval, packets per device, timeout, devices pinged at once, only devices that have a dependency,
  failed rounds before DOWN, how long a status stays valid.
- **Exclusion tag:** devices carrying this NetBox tag (default `no-ping`) are never pinged.
- **Silent-poller alert:** if a poller has not reported for the configured time (default 5 minutes) a message goes to the notification
  destinations, and another one when it is back. Use *forget* on the page for a poller that was removed on purpose.

With several pollers all of them share the same settings and the same list of targets (no sharding), so run one per network only if
the networks do not overlap.

## Zabbix (optional)

If the plugin `netbox_zabbix_status` is installed, a system job reads its host snapshot every minute: a device is DOWN when its
enabled Zabbix host has an unsuppressed problem matching `zabbix_down_patterns`. Devices with an enabled Zabbix host are skipped by
the ICMP poller. Without that plugin the Zabbix part is simply inactive and only the poller provides status.
The job also repairs a stuck NetBox *Zabbix sync* job (left in `running` after a worker restart).

## Notifications

Everything is configured in **Dependencies → Configuration → Notifications**.

![Notification settings](docs/images/notifications.png)

One message is sent per **root-cause outage** (not per affected device) once it has lasted the configured delay, and one when
the device is UP again (duration and peak impact). Several new root causes in one run are grouped into one message (10 are listed,
the rest is summarised). Every destination keeps its own delivery state: if one fails, only that one is retried (up to 10 times).
Each destination has a **Test** button that sends two clearly marked sample messages.

| Type | What to create | Payload |
|---|---|---|
| Microsoft Teams | Workflow *When a Teams webhook request is received* → *Post card in a chat or channel*, card = `triggerBody()?['card']` | `{"text", "card", "card_json"}` — Adaptive Card 1.4 |
| Slack | Slack app → *Incoming Webhooks* → add to a channel | `{"text", "attachments": [{"color", "blocks": [...]}]}` — Block Kit |
| Generic | any HTTP endpoint that accepts a JSON POST | see below |

**Generic webhook** body (`event` is `outage` or `recovery`; `test: true` for the sample messages):

```json
{"version": 1, "source": "netbox-dependencies", "event": "outage", "test": false,
 "sent_at": "2026-10-13T12:00:00+00:00", "text": "plain text summary",
 "outages": [{"device": {"id": 12, "name": "core-sw-02", "site": "HQ Campus", "url": "...", "graph_url": "..."},
              "started_at": "...", "duration_seconds": 1800, "source": "ICMP poller - no ICMP reply",
              "impact": {"down": 1, "degraded": 2, "affected": ["srv-erp-01", "dist-sw-01"]}}]}
```

`recovery` carries `recoveries: [{device, started_at, resolved_at, duration_seconds, peak_impact: {down, degraded}}]`.
If a **signing secret** is set, every request has the header `X-Signature-256: sha256=<hex HMAC-SHA256 of the raw body>` —
verify it before trusting a request.

Poller health messages use the same destinations: for the generic webhook `event` is `poller_down` or `poller_up` and the body carries
`poller: {name, last_seen, silent_seconds, url}`.

The command `manage.py dependencies_test_notifications [--destination NAME]` sends the sample messages from the command line.

## REST API

| Endpoint | |
|---|---|
| `/api/plugins/dependencies/dependencies/` | CRUD for dependencies |
| `/api/plugins/dependencies/impact/?device=<id>&device=<id>&limit=` | impact of failing devices |
| `/api/plugins/dependencies/outages/` | current root causes and explained outages |
| `/api/plugins/dependencies/status/targets/`, `.../status/report/`, `.../status/states/` | used by the poller and the graph |

## Impact rules

- No redundancy group: the failure of the upstream device takes the downstream device **down**.
- Several dependencies of one downstream device with the same `redundancy_group`: it goes down only when **all** of them are
  down; if only some are, it is **degraded** (and does not propagate further).
- Dependencies in different groups (or without a group) are independent and all mandatory.

## Security notes

- Webhook URLs and signing secrets are stored **in plaintext in the NetBox database** (many webhook URLs are bearer secrets themselves).
  They are never shown again in the UI (only a masked form), never written to the change log and never logged.
  Restrict database access accordingly and use dedicated webhooks that can be revoked.
- The poller token only needs the three permissions listed above — do not use an administrator token.
- Reporting a vulnerability: please open a private security advisory on GitHub instead of a public issue.

## Development

```bash
# inside a NetBox container / virtualenv with PostgreSQL and Redis available
python manage.py test netbox_dependencies --keepdb
```

The pure graph algorithm has tests that need no database: `netbox_dependencies/tests/test_graph.py`.

## License

[MIT](LICENSE). Bundled third-party components are listed in [NOTICE](NOTICE).
