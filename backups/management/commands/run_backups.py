import time

from django.core.management.base import BaseCommand

from backups import dump


class Command(BaseCommand):
    help = "Run the scheduled backups that are due. Call it from Task Scheduler or cron, or use --loop to keep it running."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, checking once a minute.")

    def handle(self, *args, **options):
        while True:
            for backup in dump.run_due():
                line = f"{backup.server_name} / {backup.database}: {backup.status}" + (f" ({backup.error})" if backup.error else "")
                self.stdout.write(line)
            if not options["loop"]:
                return
            time.sleep(60)
