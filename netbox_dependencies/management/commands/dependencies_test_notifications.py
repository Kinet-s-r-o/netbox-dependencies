from django.core.management.base import BaseCommand, CommandError

from netbox_dependencies.models import NotificationDestination
from netbox_dependencies.notifications import DeliveryError, send_samples


class Command(BaseCommand):
    help = 'Sends two sample messages (outage + recovery) to the enabled notification destinations.'

    def add_arguments(self, parser):
        parser.add_argument('--destination', help='Send only to the destination with this name (even if disabled).')

    def handle(self, *args, **options):
        destinations = NotificationDestination.objects.all()
        if options['destination']:
            destinations = destinations.filter(name__iexact=options['destination'])
            if not destinations.exists():
                raise CommandError(f'No destination named "{options["destination"]}".')
        else:
            destinations = destinations.filter(enabled=True)
            if not destinations.exists():
                raise CommandError('No enabled destination — add one in Dependencies > Configuration > Notifications.')
        failed = 0
        for destination in destinations:
            try:
                send_samples(destination)
            except DeliveryError as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f'{destination.name}: failed ({exc})'))
            else:
                self.stdout.write(self.style.SUCCESS(f'{destination.name}: accepted (HTTP 2xx).'))
        if failed:
            raise CommandError(f'{failed} destination(s) failed.')
