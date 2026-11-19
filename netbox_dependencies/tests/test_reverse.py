"""Reversing a dependency: upstream and downstream swap places on the same object."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from netbox_dependencies.choices import MediumChoices
from netbox_dependencies.models import Dependency
from netbox_dependencies.tests.base import Base

API = '/api/plugins/dependencies/dependencies'


class ReverseModelTests(Base):
    def test_swaps_the_ends_and_keeps_the_object(self):
        dep = self.dep('core', 'sw1')
        dep.medium = MediumChoices.OPTICAL
        dep.description = 'fibre'
        dep.save()
        dep.reverse_direction()
        dep = Dependency.objects.get(pk=dep.pk)
        self.assertEqual((dep.upstream, dep.downstream), (self.devs['sw1'], self.devs['core']))
        self.assertEqual((dep.medium, dep.description), (MediumChoices.OPTICAL, 'fibre'))
        self.assertEqual(Dependency.objects.count(), 1)

    def test_a_reversal_that_creates_a_cycle_is_refused_and_nothing_changes(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        shortcut = self.dep('core', 'ap')          # reversed it would make core depend on ap, which depends on core
        with self.assertRaises(ValidationError):
            shortcut.reverse_direction()
        self.assertEqual((shortcut.upstream, shortcut.downstream), (self.devs['core'], self.devs['ap']))
        shortcut.refresh_from_db()
        self.assertEqual((shortcut.upstream, shortcut.downstream), (self.devs['core'], self.devs['ap']))


class ReverseViewTests(Base):
    def setUp(self):
        self.client = Client()
        self.client.force_login(get_user_model().objects.create_superuser('rv', 'rv@example.com', 'x'))

    def test_api_reverses(self):
        dep = self.dep('core', 'sw1')
        response = self.client.post(f'{API}/{dep.pk}/reverse/', content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual((body['upstream']['id'], body['downstream']['id']), (self.devs['sw1'].pk, self.devs['core'].pk))

    def test_api_cycle_is_a_400_with_a_message(self):
        self.dep('core', 'sw1')
        self.dep('sw1', 'ap')
        shortcut = self.dep('core', 'ap')
        response = self.client.post(f'{API}/{shortcut.pk}/reverse/', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('cycle', str(response.json()['detail']))

    def test_api_needs_change_permission(self):
        dep = self.dep('core', 'sw1')
        client = Client()
        client.force_login(get_user_model().objects.create_user('nobody', 'n@example.com', 'x'))
        response = client.post(f'{API}/{dep.pk}/reverse/', content_type='application/json')
        self.assertEqual(response.status_code, 403)
        dep.refresh_from_db()
        self.assertEqual(dep.upstream, self.devs['core'])

    def test_detail_page_button_and_bulk_action(self):
        first, second = self.dep('core', 'sw1'), self.dep('core', 'sw2')
        page = self.client.get(first.get_absolute_url())
        self.assertContains(page, 'Reverse direction')
        self.assertContains(page, f'action="/plugins/dependencies/dependencies/{first.pk}/reverse/')
        response = self.client.post(f'/plugins/dependencies/dependencies/{first.pk}/reverse/')
        self.assertRedirects(response, first.get_absolute_url(), fetch_redirect_response=False)
        first.refresh_from_db()
        self.assertEqual(first.upstream, self.devs['sw1'])
        self.assertContains(self.client.get('/plugins/dependencies/dependencies/'), 'Reverse selected')
        self.client.post('/plugins/dependencies/dependencies/reverse/', {'pk': [first.pk, second.pk]})
        first.refresh_from_db(); second.refresh_from_db()
        self.assertEqual((first.upstream, second.upstream), (self.devs['core'], self.devs['sw2']))
