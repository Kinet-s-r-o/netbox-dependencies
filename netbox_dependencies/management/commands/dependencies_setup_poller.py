"""Prepares everything the ICMP poller container needs on the NetBox side:

  * a dedicated user (no password, not a superuser) that can do exactly what the poller does,
  * an API token for it, shown ONCE,
  * the environment file and the compose service to paste next to your NetBox.

    manage.py dependencies_setup_poller [--name NAME] [--netbox-url URL] [--write FILE]
                                        [--allowed-ip CIDR] [--rotate]

Running it again does not create a second token (a token cannot be shown twice); use --rotate to replace it."""
import os

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from users.models import ObjectPermission, Token, User

USERNAME = 'dependencies-poller'
IMAGE = 'ghcr.io/adrian-13/netbox-dependencies-poller'

# what the poller does, and nothing else
PERMISSIONS = (
    ('Dependencies poller: read devices', 'dcim', 'device', ['view']),
    ('Dependencies poller: report device status', 'netbox_dependencies', 'devicestatus', ['view', 'add', 'change']),
)


def ensure_user_and_permissions():
    user, created = User.objects.get_or_create(
        username=USERNAME, defaults={'is_active': True, 'is_superuser': False})
    if created:
        user.set_unusable_password()
        user.save()
    elif user.is_superuser:
        raise CommandError(f'The user "{USERNAME}" exists but is a superuser; refusing to use it for the poller.')
    for name, app_label, model, actions in PERMISSIONS:
        ctype = ContentType.objects.get(app_label=app_label, model=model)
        permission, _ = ObjectPermission.objects.get_or_create(name=name, defaults={'actions': actions, 'enabled': True})
        permission.actions, permission.enabled = actions, True
        permission.save()
        permission.object_types.set([ctype])
        permission.users.add(user)
    return user, created


def make_token(user, allowed_ips):
    """A new API token. v2 (hashed in the database) when NetBox has token peppers configured, otherwise a legacy v1 token."""
    version = 2 if getattr(settings, 'API_TOKEN_PEPPERS', None) else 1
    token = Token(user=user, version=version, write_enabled=True, enabled=True,
                  description='ICMP poller (created by dependencies_setup_poller)')
    if allowed_ips:
        token.allowed_ips = allowed_ips
    token.save()
    return token


def token_value(token):
    """What goes into NETBOX_TOKEN (the poller adds the right Authorization scheme itself)."""
    return f'nbt_{token.key}.{token.token}' if token.v2 else token.token


def env_file(token, name, netbox_url):
    lines = [
        '# ICMP poller of netbox-dependencies - keep this file private (it holds an API token)',
        f'NETBOX_URL={netbox_url}',
        f'NETBOX_TOKEN={token_value(token)}',
        f'POLLER_NAME={name}',
    ]
    return '\n'.join(lines) + '\n'


COMPOSE = '''\
  dependencies-poller:
    image: {image}:{version}
    restart: unless-stopped
    env_file: dependencies-poller.env     # the file written above (keep it out of git)
    cap_drop: [ALL]
    cap_add: [NET_RAW]                    # raw ICMP sockets, nothing else
    read_only: true
    security_opt: ["no-new-privileges:true"]
'''


class Command(BaseCommand):
    help = 'Creates the restricted user and API token for the ICMP poller and prints its configuration.'

    def add_arguments(self, parser):
        parser.add_argument('--name', default='netbox-poller', help='POLLER_NAME shown in NetBox (default netbox-poller)')
        parser.add_argument('--netbox-url', default='http://netbox:8080',
                            help='Address of NetBox as the poller container sees it (default http://netbox:8080)')
        parser.add_argument('--write', metavar='FILE', help='Write the environment file here (mode 600, never overwritten)')
        parser.add_argument('--allowed-ip', action='append', default=[], metavar='CIDR',
                            help='Accept the token only from this address / network (repeatable)')
        parser.add_argument('--rotate', action='store_true', help='Replace the existing token with a new one')

    @transaction.atomic
    def handle(self, *args, **options):
        from netbox_dependencies import __version__
        user, created = ensure_user_and_permissions()
        existing = Token.objects.filter(user=user)
        if existing.exists() and not options['rotate']:
            self.stdout.write(self.style.WARNING(
                f'The user "{USERNAME}" already has a token, and a token cannot be shown again.\n'
                'Use it from your existing env file, or run with --rotate to replace it (the running poller then needs the new token).'))
            return
        existing.delete()
        token = make_token(user, options['allowed_ip'])
        content = env_file(token, options['name'], options['netbox_url'].rstrip('/'))

        if options['write']:
            path = options['write']
            if os.path.exists(path):
                raise CommandError(f'{path} exists already; not overwriting it. Remove it first or print without --write.')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                handle.write(content)
            self.stdout.write(self.style.SUCCESS(f'Environment file written to {path} (mode 600).'))
        else:
            self.stdout.write('Save this as dependencies-poller.env — the token is shown only now:\n')
            self.stdout.write(content)

        self.stdout.write('Add this service to your docker-compose (next to NetBox):\n')
        self.stdout.write(COMPOSE.format(image=IMAGE, version=__version__))
        self.stdout.write(f'Then: docker compose up -d dependencies-poller   — it appears under Dependencies → ICMP poller '
                          f'as "{options["name"]}" after its first round.')
