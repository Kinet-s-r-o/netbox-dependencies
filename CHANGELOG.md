# Changelog

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
