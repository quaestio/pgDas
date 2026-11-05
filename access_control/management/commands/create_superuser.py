import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from access_control.models import Membership, Organization
from audit.models import record


class Command(BaseCommand):
    help = ("Create a Super User for an organization that has none (an organization needs at least one). Run on the server host. "
            "Asks for what is not given on the command line.")

    def add_arguments(self, parser):
        parser.add_argument("--organization", required=True, help="the organization's name")
        parser.add_argument("--email")
        parser.add_argument("--name")
        parser.add_argument("--password", help="avoid this: it stays in your shell history; leave it out to be asked")
        parser.add_argument("--force", action="store_true", help="add another Super User although the organization already has an active one")

    def handle(self, *args, **options):
        organization = Organization.objects.filter(name=options["organization"]).first()
        if organization is None:
            raise CommandError(f"There is no organization named {options['organization']!r}.")
        has_one = Membership.objects.filter(organization=organization, role="super_user", user__is_active=True).exists()
        if has_one and not options["force"]:
            raise CommandError(f"{organization.name!r} already has an active Super User. Use --force to add another.")
        email = (options["email"] or input("Email: ")).strip().lower()
        name = (options["name"] or input("Name: ")).strip()
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("That is not a valid email address.")
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError("Another account already uses this email address.")
        password = options["password"] or getpass.getpass("Password: ")
        if not options["password"] and getpass.getpass("Password (again): ") != password:
            raise CommandError("The passwords do not match.")
        try:
            validate_password(password, User(email=email, first_name=name))
        except ValidationError as error:
            raise CommandError(" ".join(error.messages))
        with transaction.atomic():
            user = User(email=email, first_name=name, is_active=True, is_staff=True, is_superuser=True)
            user.set_password(password)
            user.save()
            Membership.objects.create(user=user, organization=organization, role="super_user")
            record(None, "superuser.create", None, f"Super User {email} created for {organization.name!r} from the host",
                   payload={"organization": organization.pk, "user": user.pk})
        self.stdout.write(self.style.SUCCESS(f"Super User {email} created for {organization.name!r}."))