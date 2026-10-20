"""ICMP poller for the netbox_dependencies plugin.

Round: downloads the list of targets and the poller settings from NetBox (Dependencies -> ICMP poller),
pings the targets in parallel and sends the results back together with a heartbeat. The decision about
DOWN (failure threshold, flapping) is made by the plugin on the NetBox side — the poller sends raw results only.

The settings (interval, packets, timeout, concurrency, enabled) are read at the start of every round, so a change
in NetBox needs no restart. The environment variables below are only the fallback used until NetBox answers.

Environment variables:
  NETBOX_URL        e.g. http://netbox:8080
  NETBOX_TOKEN      API token (v1 and v2; the Bearer/Token prefix is optional)
                    (permissions: view device, view dependency, change devicestatus)
  POLLER_NAME       name shown in NetBox and used to find the poller registered there (default: the host name of the container)
  POLL_INTERVAL     fallback: seconds between rounds (default 60)
  PING_COUNT        fallback: number of ICMP packets per device (default 3)
  PING_TIMEOUT      fallback: timeout of one packet in seconds (default 1)
  PING_CONCURRENCY  fallback: how many devices are pinged at once (default 500; 100 = ~27 s for 760 dead hosts)
  VERIFY_SSL        0 disables certificate verification (default 1)
"""
import json
import logging
import os
import signal
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request

from icmplib import multiping

log = logging.getLogger('poller')

VERSION = '1.14.0'
NETBOX_URL = os.environ.get('NETBOX_URL', 'http://netbox:8080').rstrip('/')
TOKEN = os.environ.get('NETBOX_TOKEN', '')
NAME = os.environ.get('POLLER_NAME') or socket.gethostname()
BATCH = 500

# the effective settings: the environment first, replaced by what NetBox sends at the start of every round
SETTINGS = {
    'enabled': True,
    'interval_seconds': int(os.environ.get('POLL_INTERVAL', '60')),
    'ping_count': int(os.environ.get('PING_COUNT', '3')),
    'ping_timeout': float(os.environ.get('PING_TIMEOUT', '1')),
    'concurrency': int(os.environ.get('PING_CONCURRENCY', '500')),
}


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


def _heartbeat(started, targets=0, reachable=0, unreachable=0, paused=False):
    return {'name': NAME, 'version': VERSION, 'paused': paused, 'cycle_seconds': round(time.monotonic() - started, 2),
            'targets': targets, 'reachable': reachable, 'unreachable': unreachable}


def cycle():
    started = time.monotonic()
    answer = _request('GET', 'status/targets/?poller=' + urllib.parse.quote(NAME))
    for key, value in (answer.get('settings') or {}).items():
        if key in SETTINGS and value is not None:
            SETTINGS[key] = value
    targets = answer['targets']
    if not SETTINGS['enabled']:
        log.info('paused in the NetBox settings')
        _request('POST', 'status/report/', {'results': [], 'poller': _heartbeat(started, paused=True)})
        return
    results = []
    if targets:
        hosts = multiping(
            [t['ip'] for t in targets],
            count=int(SETTINGS['ping_count']),
            interval=0.2,
            timeout=float(SETTINGS['ping_timeout']),
            concurrent_tasks=int(SETTINGS['concurrency']),
            privileged=True,
        )
        by_ip = {h.address: h for h in hosts}
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
    # the heartbeat goes last, so the round time in it includes sending the results
    _request('POST', 'status/report/', {
        'results': [], 'poller': _heartbeat(started, len(results), len(results) - down, down)})
    log.info('polled %d devices (%d unreachable) in %.1fs', len(results), down, time.monotonic() - started)


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

    log.info('poller "%s" %s started', NAME, VERSION)
    while not _stop:
        began = time.monotonic()
        try:
            cycle()
        except Exception:  # the poller must survive a NetBox outage
            log.exception('poll cycle failed')
        remaining = float(SETTINGS['interval_seconds']) - (time.monotonic() - began)
        while remaining > 0 and not _stop:
            time.sleep(min(1, remaining))
            remaining -= 1


if __name__ == '__main__':
    main()
