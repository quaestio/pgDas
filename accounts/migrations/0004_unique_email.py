"""Email becomes the sign-in ID: lower-case every address, copy it into the internal username, refuse duplicates, and make it unique.

Accounts whose username is an address but which have no email get that address as their email. Accounts with no address at all
stay as they are; they cannot sign in until a Super User gives them one (python manage.py accounts_without_email lists them).
If two accounts would share an address, this stops and lists them: fix those first, then migrate again.
"""
from collections import defaultdict

from django.db import migrations, models
from django.db.models import Q
from django.db.models.functions import Lower


def normalise(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    alias = schema_editor.connection.alias
    users = list(User.objects.using(alias).all())
    by_address = defaultdict(list)
    for user in users:
        address = (user.email or "").strip().lower()
        if not address and "@" in (user.username or ""):
            address = user.username.strip().lower()
        if address:
            by_address[address].append(user)
    clashes = {address: found for address, found in by_address.items() if len(found) > 1}
    if clashes:
        lines = [f"  {address}: " + ", ".join(f"#{u.pk} ({u.username})" for u in found) for address, found in sorted(clashes.items())]
        raise RuntimeError("These email addresses are used by more than one account. Give each account its own address (or remove the "
                           "duplicate accounts), then run migrate again:\n" + "\n".join(lines))
    for address, (user,) in by_address.items():
        if user.email != address or user.username != address:
            User.objects.using(alias).filter(pk=user.pk).update(email=address, username=address)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_user_deleted_at")]
    operations = [
        migrations.RunPython(normalise, migrations.RunPython.noop),
        migrations.AddConstraint("user", models.UniqueConstraint(Lower("email"), condition=~Q(email=""), name="accounts_user_email_unique_ci")),
    ]