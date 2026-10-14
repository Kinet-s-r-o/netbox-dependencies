"""manage.py dependencies_setup_poller: a restricted user + token that really works for the poller and for nothing else."""
import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from ipam.models import IPAddress
from netbox_dependencies.models import PollerHeartbeat
from netbox_dependencies.tests.base import Base
from users.models import ObjectPermission, Token, User


def run(*args):
    out = StringIO()
    call_command('dependencies_setup_poller', *args, stdout=out)
    return out.getvalue()


def env_values(text):
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line and not line.startswith('#'))


class SetupPollerTests(Base):
    def setUp(self):
        ip = IPAddress.objects.create(address='10.0.0.1/24')
        self.devs['core'].primary_ip4 = ip
        self.devs['core'].save()
        self.dep('core', 'sw1')

    def client_for(self, token):
        client = APIClient()
        scheme = 'Bearer' if token.startswith('nbt_') else 'Token'     # the same rule as in the poller
        client.credentials(HTTP_AUTHORIZATION=f'{scheme} {token}')
        return client

    def test_token_works_for_the_poller_and_nothing_more(self):
        output = run('--name', 'p1', '--netbox-url', 'http://nb:8080/')
        values = env_values(output)
        self.assertEqual((values['NETBOX_URL'], values['POLLER_NAME']), ('http://nb:8080', 'p1'))
        self.assertGreater(len(values['NETBOX_TOKEN']), 30)
        self.assertIn('cap_drop: [ALL]', output)
        self.assertIn('ghcr.io/adrian-13/netbox-dependencies-poller:', output)
        c = self.client_for(values['NETBOX_TOKEN'])
        # what the poller does
        r = c.get('/api/plugins/dependencies/status/targets/?poller=p1')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([t['name'] for t in r.json()['targets']], ['core'])
        r = c.post('/api/plugins/dependencies/status/report/', {
            'results': [{'device': self.devs['core'].pk, 'reachable': True}],
            'poller': {'name': 'p1'}}, format='json')
        self.assertEqual((r.status_code, r.json()['updated']), (200, 1), r.content)
        self.assertTrue(PollerHeartbeat.objects.filter(name='p1').exists())
        # and nothing else
        self.assertEqual(c.post('/api/dcim/sites/', {'name': 'x', 'slug': 'x'}, format='json').status_code, 403)
        self.assertEqual(c.delete(f'/api/dcim/devices/{self.devs["sw1"].pk}/').status_code, 403)
        self.assertEqual(c.get('/api/plugins/dependencies/dependencies/').status_code, 403)
        self.assertEqual(c.get('/api/users/users/').status_code, 403)

    def test_user_is_not_privileged_and_cannot_log_in(self):
        run()
        user = User.objects.get(username='dependencies-poller')
        self.assertEqual((user.is_superuser, user.has_usable_password()), (False, False))
        self.assertEqual(ObjectPermission.objects.filter(users=user).count(), 2)

    def test_second_run_keeps_the_token_and_rotate_replaces_it(self):
        first = env_values(run())['NETBOX_TOKEN']
        again = run()
        self.assertIn('already has a token', again)
        self.assertNotIn('NETBOX_TOKEN', again)
        self.assertEqual(self.client_for(first).get('/api/plugins/dependencies/status/targets/').status_code, 200)
        second = env_values(run('--rotate'))['NETBOX_TOKEN']
        self.assertNotEqual(first, second)
        self.assertEqual(Token.objects.filter(user__username='dependencies-poller').count(), 1)
        self.assertEqual(self.client_for(first).get('/api/plugins/dependencies/status/targets/').status_code, 403)
        self.assertEqual(self.client_for(second).get('/api/plugins/dependencies/status/targets/').status_code, 200)

    def test_write_creates_a_private_file_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'poller.env')
            output = run('--write', path)
            self.assertNotIn('NETBOX_TOKEN', output)           # the token goes to the file, not to the screen
            self.assertGreater(len(env_values(open(path).read())['NETBOX_TOKEN']), 30)
            if os.name == 'posix':
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(CommandError):
                run('--rotate', '--write', path)

    def test_refuses_a_privileged_user_with_that_name(self):
        User.objects.create_superuser('dependencies-poller', 'x@example.com', 'x')
        with self.assertRaises(CommandError):
            run()
