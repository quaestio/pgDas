from django.core.management.base import BaseCommand

from access_control import readiness


class Command(BaseCommand):
    help = "Print what is set up and what is missing on this installation (the host's view: settings, folders, cron jobs), with how to fix each thing."

    def add_arguments(self, parser):
        parser.add_argument("--problems", action="store_true", help="Only warnings and failures.")

    def handle(self, *args, **options):
        marks = {"ok": "OK  ", "info": "info", "warn": "WARN", "fail": "FAIL"}
        checks = readiness.installation_checks()
        shown = [c for c in checks if not options["problems"] or c["status"] in ("warn", "fail")]
        for c in sorted(shown, key=lambda c: {"fail": 0, "warn": 1, "info": 2, "ok": 3}[c["status"]]):
            self.stdout.write(f"[{marks[c['status']]}] {c['title']}: {c['detail']}")
            if c["fix"] and c["status"] in ("warn", "fail"):
                self.stdout.write(f"        fix: {c['fix']}")
        counts = readiness.summary(checks)
        self.stdout.write(f"\n{counts['fail']} failing, {counts['warn']} warning, {counts['ok']} fine.")