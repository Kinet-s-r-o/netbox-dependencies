from django.test import TestCase

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from netbox_dependencies.models import Dependency


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name='S', slug='s')
        man = Manufacturer.objects.create(name='M', slug='m')
        dtype = DeviceType.objects.create(manufacturer=man, model='T', slug='t')
        role = DeviceRole.objects.create(name='R', slug='r')
        cls.devs = {
            n: Device.objects.create(name=n, site=site, device_type=dtype, role=role)
            for n in ('core', 'sw1', 'sw2', 'ap', 'x')
        }

    def dep(self, up, down, group=''):
        return Dependency.objects.create(
            upstream=self.devs[up], downstream=self.devs[down], redundancy_group=group
        )
