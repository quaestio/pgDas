import time

from django.core.management.base import BaseCommand

from watch import notify, sampling


class Command(BaseCommand):
    help = ("Sample the servers that are due (every PGDAS_WATCH_INTERVAL_SECONDS each, in parallel, with back-off for servers that do not answer) and send "
            "any alerts that are due (webhook / email, see .env.example). Call it from cron every minute, or use --loop.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running instead of once.")
        parser.add_argument("--no-notify", action="store_true", help="Only take samples; do not send alerts.")
        parser.add_argument("--all", action="store_true", help="Sample every server now, whether it is due or not.")
        parser.add_argument("--every", type=int, default=60, help="With --loop: seconds between checks for due servers (default 60).")

    def handle(self, *args, **options):
        while True:
            results = sampling.run_due(force=options["all"])
            if results is None:
                self.stdout.write("Another run is still in progress: nothing done.")
            for name, outcome in results or []:
                self.stdout.write(f"{name}: {outcome if isinstance(outcome, str) else str(outcome) + ' databases sampled'}")
            if not options["no_notify"]:
                sent = notify.run()
                for s in sent or []:
                    if s["new"] or s["repeated"] or s["resolved"]:
                        self.stdout.write(f"{s['server']}: {s['new']} new, {s['repeated']} reminded, {s['resolved']} resolved; "
                                          f"sent on {', '.join(s['sent']) or 'nothing'}" + "".join(f"; {c} failed: {r}" for c, r in s["failed"]))
            if not options["loop"]:
                return
            time.sleep(max(15, options["every"]))