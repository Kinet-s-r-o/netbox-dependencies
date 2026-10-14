"""Message formats per destination type: Teams (Adaptive Card), Slack (Block Kit), generic webhook (JSON).

Pure functions over "view models" (dicts) prepared by notifications.py — nothing here touches the database:

  outage  : {device_id, name, site, since, down, degraded, names[], source, affected_pks(set), graph_url, device_url}
  recovery: {device_id, name, site, since, until, down, degraded, graph_url, device_url}

`render_outage(kind, views, now, test=False)` and `render_recovery(...)` return a JSON-serialisable payload.
With more than MAX_ROOTS root causes only the first MAX_ROOTS (largest impact) are listed and the rest is summarised."""
import json
from datetime import timedelta

from django.utils import timezone

MAX_ROOTS = 10     # root causes listed in one message (size limits of Teams / Slack)
MAX_LISTED = 8     # affected devices listed for one root cause


# ------------------------------------------------------------------ shared helpers
def local(dt):
    return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M %Z')


def since(dt, now):
    """"12:16" for today, otherwise "10-12 12:16"."""
    local_dt, today = timezone.localtime(dt), timezone.localtime(now).date()
    return local_dt.strftime('%H:%M') if local_dt.date() == today else local_dt.strftime('%m-%d %H:%M')


def duration(delta):
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {minutes} min'
    return f'{minutes} min'


def plural(n, one, many):
    return one if n == 1 else many


def _roots_word(n):
    return plural(n, 'root cause', 'root causes')


def _affected_phrase(views):
    """"8 devices down in total" — a device is never counted twice."""
    total = len(set().union(*[v['affected_pks'] for v in views])) if views else 0
    return total, f'{total} {plural(total, "device", "devices")} down in total'


def _ordered(views):
    return sorted(views, key=lambda v: (-v['down'], v['name']))


def _shown_names(view):
    shown = view['names'][:MAX_LISTED]
    if len(view['names']) > MAX_LISTED:
        shown.append(f'… and {len(view["names"]) - MAX_LISTED} more')
    return shown


def _degraded_word(n):
    return f'{n} degraded'


def outage_text(views):
    """Plain text (fallback for clients without rich cards and the `text` field of the payload)."""
    n = len(views)
    head = f'🔴 Outage: {views[0]["name"]}' if n == 1 else f'🔴 Outages: {n} {_roots_word(n)}'
    lines = [
        f'{v["name"]} ({v["site"]}): {v["down"]} down' + (f', {_degraded_word(v["degraded"])}' if v['degraded'] else '')
        for v in _ordered(views)[:MAX_ROOTS]
    ]
    return head + '\n' + '\n'.join(lines) + (f'\n… and {n - MAX_ROOTS} more' if n > MAX_ROOTS else '')


def recovery_text(views):
    n = len(views)
    head = f'🟢 Outage ended: {views[0]["name"]}' if n == 1 else f'🟢 Outages ended: {n} devices'
    lines = [f'{v["name"]} ({v["site"]}): lasted {duration(v["until"] - v["since"])}' for v in views[:MAX_ROOTS]]
    return head + '\n' + '\n'.join(lines) + (f'\n… and {n - MAX_ROOTS} more' if n > MAX_ROOTS else '')


def sample_views(now, home_url=''):
    """Made-up devices for the sample messages."""
    started = now - timedelta(minutes=47)
    outage = [
        {'device_id': 0, 'name': 'core-router-01', 'site': 'Sample site', 'since': started, 'down': 6,
         'degraded': 1, 'names': ['sw-access-01', 'sw-access-02', 'ap-hall-01', 'ap-hall-02', 'ap-office-01',
                                  'ap-warehouse-01 (degraded)'],
         'source': 'ICMP poller — no ICMP reply', 'affected_pks': {1, 2, 3, 4, 5, 6, 7}, 'graph_url': home_url,
         'device_url': ''},
        {'device_id': 0, 'name': 'ap-reception-01', 'site': 'Sample site', 'since': started + timedelta(minutes=20),
         'down': 0, 'degraded': 0, 'names': [], 'source': 'Zabbix — Unavailable by ICMP ping',
         'affected_pks': {8}, 'graph_url': home_url, 'device_url': ''},
    ]
    recovery = [{'device_id': 0, 'name': 'core-router-01', 'site': 'Sample site', 'since': started, 'until': now,
                 'down': 6, 'degraded': 1, 'graph_url': home_url, 'device_url': ''}]
    return outage, recovery


# ================================================================== Microsoft Teams (Adaptive Card 1.4)
def _t_text(text, **kwargs):
    block = {'type': 'TextBlock', 'text': text, 'wrap': True}
    block.update(kwargs)
    return block


def _t_header(emoji, title, subtitle, style, color):
    return {
        'type': 'Container', 'style': style, 'bleed': False,
        'items': [{
            'type': 'ColumnSet',
            'columns': [
                {'type': 'Column', 'width': 'auto', 'verticalContentAlignment': 'Center',
                 'items': [_t_text(emoji, size='ExtraLarge')]},
                {'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
                 'items': [_t_text(title, size='Large', weight='Bolder', color=color),
                           _t_text(subtitle, isSubtle=True, spacing='None')]},
            ],
        }],
    }


def _t_right(big, caption, color='Default', extra=None):
    items = [
        _t_text(big, size='ExtraLarge', weight='Bolder', color=color, horizontalAlignment='Right'),
        _t_text(caption, size='Small', isSubtle=True, spacing='None', horizontalAlignment='Right'),
    ]
    if extra:
        items.append(_t_text(extra, size='Small', color='Warning', spacing='None', horizontalAlignment='Right'))
    return {'type': 'Column', 'width': 'auto', 'items': items}


def _t_item(name, lines, right, links):
    """One tile (a device): the name on the left, a number on the right, lines and buttons below."""
    items = [{
        'type': 'ColumnSet',
        'columns': [
            {'type': 'Column', 'width': 'stretch', 'verticalContentAlignment': 'Center',
             'items': [_t_text(name, size='Medium', weight='Bolder')]},
            right,
        ],
    }]
    items.extend(lines)
    actions = [{'type': 'Action.OpenUrl', 'title': title, 'url': url} for title, url in links if url]
    if actions:
        items.append({'type': 'ActionSet', 'actions': actions, 'spacing': 'Small'})
    return {'type': 'Container', 'style': 'emphasis', 'spacing': 'Medium', 'items': items}


def _t_footer(now, test):
    label = 'NetBox Dependencies · sample notification' if test else 'NetBox Dependencies'
    return _t_text(f'{label} · {local(now)}', size='Small', isSubtle=True, spacing='Medium', separator=True)


def _t_banner():
    return {'type': 'Container', 'style': 'warning', 'spacing': 'Small',
            'items': [_t_text('🧪 Sample of the notification layout — this is not a real outage.',
                              size='Small', weight='Bolder')]}


def _t_card(body):
    return {'type': 'AdaptiveCard', '$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
            'version': '1.4', 'msteams': {'width': 'Full'}, 'body': body}


def _teams_outage(views, now, test):
    total, phrase = _affected_phrase(views)
    n = len(views)
    body = [_t_header('🔴', 'Outage', f'{n} {_roots_word(n)} · {phrase}', 'attention', 'Attention')]
    if test:
        body.append(_t_banner())
    for v in _ordered(views)[:MAX_ROOTS]:
        lines = [
            _t_text(f'📍 {v["site"]}   ·   {v["source"]}', isSubtle=True, size='Small', spacing='None'),
            _t_text(f'🕒 since {since(v["since"], now)}   ·   lasting {duration(now - v["since"])}',
                    isSubtle=True, size='Small', spacing='None'),
        ]
        shown = _shown_names(v)
        if shown:
            lines.append(_t_text('Affected: ' + ', '.join(shown), size='Small', spacing='Small'))
        else:
            lines.append(_t_text('No further impact on other devices.', size='Small', isSubtle=True, spacing='Small'))
        has_impact = v['down'] or v['degraded']
        right = _t_right(str(v['down']) if has_impact else '—', 'down' if has_impact else 'no impact',
                         color='Attention' if v['down'] else 'Default',
                         extra=_degraded_word(v['degraded']) if v['degraded'] else None)
        body.append(_t_item(v['name'], lines, right, [('Open in graph', v['graph_url']), ('Device', v['device_url'])]))
    if n > MAX_ROOTS:
        body.append(_t_text(f'… and {n - MAX_ROOTS} more {_roots_word(n - MAX_ROOTS)} (see the graph).',
                            isSubtle=True, spacing='Medium'))
    body.append(_t_footer(now, test))
    card = _t_card(body)
    return {'text': outage_text(views), 'card': card, 'card_json': json.dumps(card, ensure_ascii=False)}


def _teams_recovery(views, now, test):
    n = len(views)
    body = [_t_header('🟢', 'Outage ended',
                      f'{n} {plural(n, "device is", "devices are")} back in service', 'good', 'Good')]
    if test:
        body.append(_t_banner())
    for v in views[:MAX_ROOTS]:
        impact = (f'{v["down"]} down' + (f', {_degraded_word(v["degraded"])}' if v['degraded'] else '')
                  if v['down'] or v['degraded'] else 'no further impact')
        lines = [
            _t_text(f'📍 {v["site"]}', isSubtle=True, size='Small', spacing='None'),
            _t_text(f'🕒 {local(v["since"])}  →  {local(v["until"])}', isSubtle=True, size='Small', spacing='None'),
            _t_text(f'Maximum impact: {impact}', size='Small', spacing='Small'),
        ]
        body.append(_t_item(v['name'], lines, _t_right(duration(v['until'] - v['since']), 'duration', color='Good'),
                            [('Open in graph', v['graph_url']), ('Device', v['device_url'])]))
    if n > MAX_ROOTS:
        body.append(_t_text(f'… and {n - MAX_ROOTS} more.', isSubtle=True, spacing='Medium'))
    body.append(_t_footer(now, test))
    card = _t_card(body)
    return {'text': recovery_text(views), 'card': card, 'card_json': json.dumps(card, ensure_ascii=False)}


# ================================================================== Slack (Block Kit inside an attachment → coloured bar)
def _esc(text):
    """Slack mrkdwn: &, < and > must be escaped, otherwise a device name could turn into a link / mention."""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _s_buttons(links):
    elements = [{'type': 'button', 'text': {'type': 'plain_text', 'text': title}, 'url': url}
                for title, url in links if url]
    return {'type': 'actions', 'elements': elements} if elements else None


def _s_message(text, color, header, subtitle, tiles, footer):
    blocks = [{'type': 'header', 'text': {'type': 'plain_text', 'text': header[:150], 'emoji': True}},
              {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': _esc(subtitle)}]}]
    blocks.extend(tiles)
    blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': _esc(footer)}]})
    # `text` is the notification fallback and is read as mrkdwn too; Slack allows at most 50 blocks
    return {'text': _esc(text), 'attachments': [{'color': color, 'blocks': blocks[:50]}]}


def _s_banner():
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '🧪 _Sample of the notification layout — this is not a real outage._'}}


def _slack_outage(views, now, test):
    n = len(views)
    total, phrase = _affected_phrase(views)
    tiles = [_s_banner()] if test else []
    for v in _ordered(views)[:MAX_ROOTS]:
        impact = (f'*{v["down"]}* down' + (f' · {_degraded_word(v["degraded"])}' if v['degraded'] else '')
                  if v['down'] or v['degraded'] else 'no impact')
        text = (f'*{_esc(v["name"])}*   —   {impact}\n'
                f'📍 {_esc(v["site"])}  ·  {_esc(v["source"])}\n'
                f'🕒 since {since(v["since"], now)}  ·  lasting {duration(now - v["since"])}')
        tiles.append({'type': 'divider'})
        tiles.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': text[:3000]}})
        shown = _shown_names(v)
        tiles.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': (
            'Affected: ' + _esc(', '.join(shown)) if shown else 'No further impact on other devices.')[:3000]}]})
        buttons = _s_buttons([('Open in graph', v['graph_url']), ('Device', v['device_url'])])
        if buttons:
            tiles.append(buttons)
    if n > MAX_ROOTS:
        tiles.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f'… and {n - MAX_ROOTS} more {_roots_word(n - MAX_ROOTS)} (see the graph).'}]})
    return _s_message(outage_text(views), '#d63939', '🔴 Outage', f'{n} {_roots_word(n)} · {phrase}', tiles,
                      f'NetBox Dependencies{" · sample notification" if test else ""} · {local(now)}')


def _slack_recovery(views, now, test):
    n = len(views)
    tiles = [_s_banner()] if test else []
    for v in views[:MAX_ROOTS]:
        impact = (f'{v["down"]} down' + (f', {_degraded_word(v["degraded"])}' if v['degraded'] else '')
                  if v['down'] or v['degraded'] else 'no further impact')
        text = (f'*{_esc(v["name"])}*   —   lasted *{duration(v["until"] - v["since"])}*\n'
                f'📍 {_esc(v["site"])}\n🕒 {local(v["since"])}  →  {local(v["until"])}\nMaximum impact: {impact}')
        tiles.append({'type': 'divider'})
        tiles.append({'type': 'section', 'text': {'type': 'mrkdwn', 'text': text[:3000]}})
        buttons = _s_buttons([('Open in graph', v['graph_url']), ('Device', v['device_url'])])
        if buttons:
            tiles.append(buttons)
    if n > MAX_ROOTS:
        tiles.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f'… and {n - MAX_ROOTS} more.'}]})
    return _s_message(recovery_text(views), '#2fb344', '🟢 Outage ended',
                      f'{n} {plural(n, "device is", "devices are")} back in service', tiles,
                      f'NetBox Dependencies{" · sample notification" if test else ""} · {local(now)}')


# ================================================================== generic webhook (JSON event)
def _g_base(event, text, now, test):
    return {'version': 1, 'source': 'netbox-dependencies', 'event': event, 'test': bool(test),
            'sent_at': now.isoformat(), 'text': text}


def _g_device(v):
    return {'id': v['device_id'] or None, 'name': v['name'], 'site': v['site'],
            'url': v['device_url'] or None, 'graph_url': v['graph_url'] or None}


def _generic_outage(views, now, test):
    payload = _g_base('outage', outage_text(views), now, test)
    payload['outages'] = [{
        'device': _g_device(v),
        'started_at': v['since'].isoformat(),
        'duration_seconds': max(0, int((now - v['since']).total_seconds())),
        'source': v['source'],
        'impact': {'down': v['down'], 'degraded': v['degraded'], 'affected': v['names']},
    } for v in _ordered(views)]
    return payload


def _generic_recovery(views, now, test):
    payload = _g_base('recovery', recovery_text(views), now, test)
    payload['recoveries'] = [{
        'device': _g_device(v),
        'started_at': v['since'].isoformat(),
        'resolved_at': v['until'].isoformat(),
        'duration_seconds': max(0, int((v['until'] - v['since']).total_seconds())),
        'peak_impact': {'down': v['down'], 'degraded': v['degraded']},
    } for v in views]
    return payload


# ================================================================== entry points
RENDERERS = {
    'teams': (_teams_outage, _teams_recovery),
    'slack': (_slack_outage, _slack_recovery),
    'generic': (_generic_outage, _generic_recovery),
}


def render_outage(kind, views, now, test=False):
    return RENDERERS[kind][0](views, now, test)


def render_recovery(kind, views, now, test=False):
    return RENDERERS[kind][1](views, now, test)


def render_samples(kind, now, home_url=''):
    """A sample outage and its recovery (made-up devices, marked as a test) for the given destination type."""
    outage, recovery = sample_views(now, home_url)
    return [render_outage(kind, outage, now, test=True), render_recovery(kind, recovery, now, test=True)]
