import secrets
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from access_control.models import Installation


class Command(BaseCommand):
    help = "Create a one-time first-installation token; run on the server host."
    def handle(self, *args, **options):
        if Installation.objects.filter(pk=1, completed=True).exists():
            raise CommandError("Installation is already complete. Use changepassword for account recovery.")
        path = Path(settings.BASE_DIR) / "data" / "installation.token"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("x") as file:
                file.write(secrets.token_urlsafe(32))
            path.chmod(0o600)
        self.stdout.write(path.read_text())
