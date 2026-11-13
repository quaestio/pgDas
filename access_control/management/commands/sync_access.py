from django.core.management.base import BaseCommand

from access_control import serverwide


class Command(BaseCommand):
    help = "Give people with server-wide access the databases created since the last sync. Run it from a schedule to follow new databases."

    def handle(self, *args, **options):
        self.stdout.write(f"{serverwide.sync_all()} database grant(s) added.")