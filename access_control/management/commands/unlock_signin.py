from django.core.management.base import BaseCommand, CommandError

from access_control.models import SignInFailure


class Command(BaseCommand):
    help = "Lift a sign-in lockout: for one email address, one network address, or everything. Run on the server host."

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?", help="the email address to unlock")
        parser.add_argument("--address", help="unlock this network address instead")
        parser.add_argument("--all", action="store_true", help="forget every recorded failed sign-in")

    def handle(self, *args, **options):
        if options["all"]:
            count, _ = SignInFailure.objects.all().delete()
        elif options["address"]:
            count, _ = SignInFailure.objects.filter(address=options["address"]).delete()
        elif options["email"]:
            count, _ = SignInFailure.objects.filter(email=options["email"].strip().lower()).delete()
        else:
            raise CommandError("Give an email address, --address <address> or --all.")
        self.stdout.write(f"{count} recorded failed sign-in(s) removed.")