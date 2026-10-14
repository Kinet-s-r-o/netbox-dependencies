# Changelog

## 1.9.0 — 2026-10-14
- **Export for import** button on the dependency list: a CSV (respecting the list filters) in the exact columns the Import page reads,
  so a list can be exported, edited and imported again, or moved to another NetBox.

## 1.8.0 — 2026-10-14
- New look of the **Dependency tree**: rows with a round status disc holding the device icon, guide lines between levels, chips for
  the link medium (with the same line pattern as in the graph), redundancy group and multiple upstream, a "Down" highlight, counters of
  direct / total dependents, edit and delete actions on hover. Works in the dark theme.
- The device icon set and its selection rules now live in one place (`icons.py`) and are shared by the tree and the graph.

## 1.7.0 — 2026-10-14
- *Recent notifications* is paginated (it showed only the last 15) and can be filtered: ongoing outages, ended, not delivered,
  and a device-name search.

## 1.6.0 — 2026-10-14
- `manage.py dependencies_setup_poller`: creates a restricted user (read devices, report status — nothing else), its API token and the
  environment file / compose service for the poller. The token is shown once; the file is written with mode 600 and never overwritten;
  `--rotate` replaces the token, `--allowed-ip` restricts where it works.
- The poller image is built by GitHub Actions on every release and published as `ghcr.io/adrian-13/netbox-dependencies-poller:<version>`
  (amd64 and arm64); no clone or build needed. The example compose service runs it with all capabilities dropped except `NET_RAW`
  and a read-only filesystem.

## 1.5.0 — 2026-10-14
- Pollers can be registered, edited and unregistered on the ICMP poller page (*Add poller*): description, enabled, **scope (sites)** and
  own interval / packets / timeout / concurrency. A poller with sites pings only those sites and the others skip them, so several
  pollers never ping the same device. Pollers that merely report show as *auto-detected* with a *Register* button.
- The poller sends its name (`?poller=`) when it asks for targets. Update the poller container together with the plugin.
- Migration 0011.

## 1.4.0 — 2026-10-14
- New page **Dependencies → Configuration → ICMP poller**: the state of every poller (last report, round time, devices up / down)
  and its settings. The poller downloads the settings every round, so changing the interval, packets, timeout, concurrency,
  failure threshold or pausing it needs no container restart. Environment variables are only the fallback.
- Devices carrying the exclusion tag (default `no-ping`) are never pinged; "only devices with a dependency" is now a setting too.
- The poller sends a heartbeat after every round. A poller silent for longer than the limit raises one alert to the notification
  destinations (Teams / Slack / generic webhook), and a message when it is back.
- Poller: `POLLER_NAME` variable. Poller image / plugin must be updated together (the poller now reads its settings from the plugin).
- Migration 0010 (two new tables).

## 1.3.0 — 2026-10-14
- Link media: added VPN / tunnel, LTE / 5G and satellite (each with its own colour and line pattern in the graph, legend,
  filters and the right-click menu). No migration needed.
- Click a link in the graph to see its details (medium, type, redundancy group, description) with Open / Edit buttons.

## 1.2.0 — 2026-10-14
- More device icons (15): firewall, router, switch, Wi-Fi access point, base station, radio link / CPE, satellite, server, storage,
  camera, VoIP phone, printer, UPS, PDU, media converter. The icon is picked from the device role, then the model, then the name;
  the Legend lists them.
- Links in Teams / Slack messages now open the graph with that outage already simulated and the camera framed on the root cause
  and the devices it takes down (`?focus=<device>&impact=1`).
- The overview minimap follows the dark theme (also when the theme is switched).

## 1.1.1 — 2026-10-14
- The graph canvas is plain again (the dot grid from 1.1.0 is removed).

## 1.1.0 — 2026-10-14
- New graph design: device cards with a round status badge and role icon, smooth S-curved links, a dot-grid canvas,
  soft shadows, pulsing root causes, link highlight on hover, and light / dark theme that follows NetBox (also live).
- Clouds (other locations) redrawn as outlined, softly tinted shapes.
- Notification times now show the time zone (NetBox `TIME_ZONE`), e.g. `2026-10-14 08:00 CEST`.

## 1.0.1 — 2026-10-14
- Fix: notifications were also sent for devices that are down but not part of any dependency. Only devices that
  take part in at least one dependency are announced now.

## 1.0.0 — 2026-10-13
First public release.

- Dependency model (upstream → downstream, link medium, redundancy groups) with validation (no cycles, no duplicates).
- Dependency tree, device tab, outage simulation, REST API and CSV import.
- Interactive Cytoscape.js graph: maps per site / region / custom map, clouds for other locations, search, filters,
  role icons, link media (Ethernet / optical / radio), automatic layout saving, PNG export, minimap.
- Live device status from Zabbix (optional `netbox_zabbix_status` plugin) and from a built-in ICMP poller container;
  root-cause analysis (root causes vs. outages explained by an upstream failure); watchdog for a stuck Zabbix sync.
- Notifications to any number of destinations — Microsoft Teams (Adaptive Card), Slack (Block Kit) and a generic
  JSON webhook with optional HMAC-SHA256 signature; outage and recovery messages, per-destination retries.
- Pagination and limits for large installations.
