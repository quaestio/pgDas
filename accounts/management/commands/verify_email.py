from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Mark an email address as verified by hand (for an account that cannot receive the verification email)."

    def add_arguments(self, parser):
        parser.add_argument("email", nargs="?")
        parser.add_argument("--all", action="store_true", help="Mark every account verified.")

    def handle(self, *args, **options):
        accounts = get_user_model().objects.exclude(email="")
        if options["all"]:
            count = accounts.update(email_verified=True)
        elif options["email"]:
            count = accounts.filter(email__iexact=options["email"].strip()).update(email_verified=True)
            if not count:
                raise CommandError("no account has that email address")
        else:
            raise CommandError("give an email address or --all")
        self.stdout.write(f"{count} account(s) marked verified.")