from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "List the accounts that have no email address. Email is the only sign-in ID, so these cannot sign in until a Super User gives them one (Employees > Edit)."

    def handle(self, *args, **options):
        missing = get_user_model().objects.filter(email="", deleted_at__isnull=True).order_by("pk")
        for user in missing:
            self.stdout.write(f"#{user.pk}  {user.username}  {'active' if user.is_active else 'disabled'}")
        self.stdout.write(f"{missing.count()} account(s) without an email address.")