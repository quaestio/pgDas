from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from access_control.models import Membership, Organization, OrganizationServer
from audit.models import record


class Command(BaseCommand):
    help = ("List the organizations of this installation, or switch one off or on, or remove registrations that were never confirmed. "
            "Run on the server host: no one inside PGDAS can see across organizations.")

    def add_arguments(self, parser):
        parser.add_argument("--disable", metavar="NAME", help="switch an organization off: its people can no longer use PGDAS")
        parser.add_argument("--enable", metavar="NAME", help="switch an organization back on")
        parser.add_argument("--purge-pending", type=int, metavar="HOURS",
                            help="delete organizations registered more than HOURS ago whose email link was never opened")

    def handle(self, *args, **options):
        if options["disable"] or options["enable"]:
            name, on = options["enable"] or options["disable"], bool(options["enable"])
            organization = Organization.objects.filter(name=name).first()
            if organization is None:
                raise CommandError(f"There is no organization named {name!r}.")
            organization.active = on
            organization.save(update_fields=["active"])
            record(None, "organization.enable" if on else "organization.disable", None, f"Organization {name!r} switched {'on' if on else 'off'} from the host",
                   payload={"organization": organization.pk})
            self.stdout.write(self.style.SUCCESS(f"{name!r} is now {'on' if on else 'off'}."))
            return
        if options["purge_pending"] is not None:
            self.purge(options["purge_pending"])
            return
        self.list()

    def list(self):
        self.stdout.write(f"{'ID':>4}  {'ACTIVE':<7} {'PEOPLE':>6} {'SUPER USERS':>11} {'SERVERS':>7}  NAME")
        for organization in Organization.objects.order_by("pk"):
            people = Membership.objects.filter(organization=organization)
            supers = people.filter(role="super_user", user__is_active=True).count()
            servers = OrganizationServer.objects.filter(organization=organization).count()
            self.stdout.write(f"{organization.pk:>4}  {'yes' if organization.active else 'NO':<7} {people.count():>6} {supers:>11} {servers:>7}  {organization.name}")

    def purge(self, hours):
        cutoff = timezone.now() - timedelta(hours=hours)
        removed = 0
        for organization in Organization.objects.filter(active=False):
            members = list(Membership.objects.filter(organization=organization).select_related("user"))
            waiting = members and all(not m.user.is_active and m.user.date_joined < cutoff for m in members)
            if not waiting or OrganizationServer.objects.filter(organization=organization).exists():
                continue
            with transaction.atomic():
                user_ids = [m.user_id for m in members]
                Membership.objects.filter(organization=organization).delete()
                get_user_model().objects.filter(pk__in=user_ids).delete()
                record(None, "organization.purge", None, f"Unconfirmed registration of {organization.name!r} removed", payload={"organization": organization.pk})
                organization.delete()
            removed += 1
        self.stdout.write(f"{removed} unconfirmed registration(s) removed.")