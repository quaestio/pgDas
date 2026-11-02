import time

from django.core.management.base import BaseCommand

from watch import notify, sampling


class Command(BaseCommand):
    help = ("Take a size sample of every database of every saved server, and send any alerts that are due (webhook / email, "
            "see .env.example). Call it from Task Scheduler or cron, or use --loop.")

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running instead of once.")
        parser.add_argument("--no-notify", action="store_true", help="Only take samples; do not send alerts.")
        parser.add_argument("--every", type=int, default=300, help="With --loop: seconds between samples (default 300).")

    def handle(self, *args, **options):
        while True:
            for name, outcome in sampling.sample_all():
                self.stdout.write(f"{name}: {outcome if isinstance(outcome, str) else str(outcome) + ' databases sampled'}")
            if not options["no_notify"]:
                sent = notify.run()
                for s in sent or []:
                    if s["new"] or s["repeated"] or s["resolved"]:
                        self.stdout.write(f"{s['server']}: {s['new']} new, {s['repeated']} reminded, {s['resolved']} resolved; "
                                          f"sent on {', '.join(s['sent']) or 'nothing'}" + "".join(f"; {c} failed: {r}" for c, r in s["failed"]))
            if not options["loop"]:
                return
            time.sleep(max(30, options["every"]))
