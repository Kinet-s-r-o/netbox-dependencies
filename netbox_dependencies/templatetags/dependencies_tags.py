"""Small template tags for the dependency tree: the status disc with the device icon and the link-medium swatch."""
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from ..icons import PATHS, icon_for

register = template.Library()

STATE_CLASS = {'up': 'up', 'down': 'down', 'unknown': 'unknown', 'suppressed': 'suppressed'}

# colour and dash pattern of every link medium - the same values the graph uses
MEDIUM_STYLE = {
    'ethernet': ('#4299e1', '', 2.5),
    'optical': ('#f59f00', '', 3),
    'radio': ('#22b8cf', '7 5', 2.5),
    'vpn': ('#e64980', '2 5', 2.5),
    'lte': ('#12b886', '10 4 2 4', 2.5),
    'satellite': ('#845ef7', '16 6', 2.5),
    'unspecified': ('#868e96', '', 2),
}


@register.simple_tag
def dep_disc(device, state):
    """A round status-coloured disc holding the icon of the device (a dot when no icon matches)."""
    key = icon_for(device.role.name if device.role_id else '', device.device_type.model if device.device_type_id else '',
                   device.name or '')
    css = STATE_CLASS.get(state, 'none')
    if key:
        glyph = format_html('<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{}"/></svg>', PATHS[key])
    else:
        glyph = mark_safe('<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.5"/></svg>')
    title = {'up': 'Up', 'down': 'Down', 'unknown': 'Unknown', 'suppressed': 'Suppressed'}.get(state, 'No status')
    return format_html('<span class="dep-disc dep-disc-{}" title="{}">{}</span>', css, title, glyph)


@register.simple_tag
def dep_medium(medium, label):
    """A short sample of the line the graph draws for this medium, followed by its name."""
    colour, dash, width = MEDIUM_STYLE.get(medium, MEDIUM_STYLE['unspecified'])
    line = format_html(
        '<svg width="26" height="10" viewBox="0 0 26 10" aria-hidden="true"><line x1="1" y1="5" x2="25" y2="5" '
        'stroke="{}" stroke-width="{}" stroke-linecap="{}"{}/></svg>',
        colour, width, 'round' if medium == 'vpn' else 'butt',
        format_html(' stroke-dasharray="{}"', dash) if dash else '')
    return format_html('<span class="dep-chip">{}{}</span>', line, label)
