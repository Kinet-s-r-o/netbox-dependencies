"""ICMP poller for the netbox_dependencies plugin.

Cycle: downloads the list of targets from NetBox (devices without Zabbix monitoring),
pings them in parallel and sends the results back. The decision about DOWN (failure threshold,
flapping) is made by the plugin on the NetBox side — the poller sends raw results only.

Environment variables:
  NETBOX_URL        e.g. http://netbox:8080
  NETBOX_TOKEN      API token (v1 and v2; the Bearer/Token prefix is optional)
                    (permissions: view device, view dependency, change devicestatus)
  POLL_INTERVAL     seconds between cycles (default 60)
  PING_COUNT        number of ICMP packets per device (default 3)
  PING_TIMEOUT      timeout of one packet in seconds (default 1)
  ONLY_IN_GRAPH     1 = ping only devices in the dependency tree (default 1)
  PING_CONCURRENCY  how many devices are pinged at once (default 500; 100 = ~27 s for 760 dead hosts)
  VERIFY_SSL        0 disables certificate verification (default 1)
"""
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import ssl

from icmplib import multiping

log = logging.getLogger('poller')

NETBOX_URL = os.environ.get('NETBOX_URL', 'http://netbox:8080').rstrip('/')
TOKEN = os.environ.get('NETBOX_TOKEN', '')
INTERVAL = int(os.environ.get('POLL_INTERVAL', '60'))
COUNT = int(os.environ.get('PING_COUNT', '3'))
TIMEOUT = float(os.environ.get('PING_TIMEOUT', '1'))
ONLY_IN_GRAPH = os.environ.get('ONLY_IN_GRAPH', '1') == '1'
CONCURRENCY = int(os.environ.get('PING_CONCURRENCY', '500'))
BATCH = 500



def _auth_header(raw):
    """Accepts all usual forms of the token: v1 (`<token>`), v2 (`nbt_<key>.<token>`)
    and a value copied with its prefix (`Bearer ...` / `Token ...`)."""
    raw = raw.strip().strip('"\'')
    if raw.lower().startswith(('bearer ', 'token ')):
        scheme, _, value = raw.partition(' ')
        return f'{scheme.capitalize()} {value.strip()}'
    return f'Bearer {raw}' if raw.startswith('nbt_') else f'Token {raw}'


AUTH_HEADER = _auth_header(TOKEN)
_ctx = None
if os.environ.get('VERIFY_SSL', '1') == '0':
    _ctx = ssl.create_default_context()
    _ctx.check_hostname = False
    _ctx.verify_mode = ssl.CERT_NONE

_stop = False


def _request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f'{NETBOX_URL}/api/plugins/dependencies/{path}',
        data=data,
        method=method,
        headers={
            'Authorization': AUTH_HEADER,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=_ctx) as resp:
        return json.load(resp)


def cycle():
    query = '?in_graph=1' if ONLY_IN_GRAPH else ''
    targets = _request('GET', f'status/targets/{query}')['targets']
    if not targets:
        log.info('no targets')
        return
    started = time.monotonic()
    hosts = multiping(
        [t['ip'] for t in targets],
        count=COUNT,
        interval=0.2,
        timeout=TIMEOUT,
        concurrent_tasks=CONCURRENCY,
        privileged=True,
    )
    by_ip = {h.address: h for h in hosts}
    results = []
    for t in targets:
        host = by_ip.get(t['ip'])
        alive = bool(host and host.is_alive)
        detail = (
            f'rtt {host.avg_rtt:.1f} ms, loss {host.packet_loss:.0%}' if alive
            else 'no ICMP reply'
        )
        results.append({'device': t['id'], 'reachable': alive, 'detail': detail})
    down = sum(1 for r in results if not r['reachable'])
    for i in range(0, len(results), BATCH):
        _request('POST', 'status/report/', {'results': results[i:i + BATCH]})
    log.info('polled %d devices (%d unreachable) in %.1fs',
             len(results), down, time.monotonic() - started)


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if not TOKEN:
        log.error('NETBOX_TOKEN is not set')
        sys.exit(1)

    def _handle(*_):
        global _stop
        _stop = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    while not _stop:
        began = time.monotonic()
        try:
            cycle()
        except Exception:  # the poller must survive a NetBox outage
            log.exception('poll cycle failed')
        remaining = INTERVAL - (time.monotonic() - began)
        while remaining > 0 and not _stop:
            time.sleep(min(1, remaining))
            remaining -= 1


if __name__ == '__main__':
    main()
