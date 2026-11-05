from django.conf import settings
from django.db import migrations, models


def bootstrap(apps, schema_editor):
    alias = schema_editor.connection.alias
    User = apps.get_model(settings.AUTH_USER_MODEL)
    Organization = apps.get_model("access_control", "Organization")
    Membership = apps.get_model("access_control", "Membership")
    Scope = apps.get_model("access_control", "OrganizationServer")
    Installation = apps.get_model("access_control", "Installation")
    users = User.objects.using(alias)
    completed = users.filter(is_superuser=True, is_active=True).exists()
    Installation.objects.using(alias).get_or_create(pk=1, defaults={"completed": completed})
    if users.exists():
        org = Organization.objects.using(alias).first() or Organization.objects.using(alias).create(name="My organization")
        for user in users.all():
            Membership.objects.using(alias).get_or_create(user_id=user.pk, defaults={"organization_id": org.pk,
                "role": "super_user" if user.is_superuser else ("editor" if user.is_staff else "viewer")})
        for server in apps.get_model("servers", "ServerConnection").objects.using(alias).all():
            Scope.objects.using(alias).get_or_create(server_id=server.pk, defaults={"organization_id": org.pk})


class Migration(migrations.Migration):
    dependencies = [("access_control", "0001_initial"), ("accounts", "0002_user_designation")]
    operations = [
        migrations.AddField("databasegrant", "execution_username", models.CharField(max_length=100, blank=True)),
        migrations.AddField("databasegrant", "execution_password", models.TextField(blank=True)),
        migrations.CreateModel(name="Installation", fields=[("id", models.BigAutoField(primary_key=True, serialize=False)),
                                                            ("completed", models.BooleanField(default=False))]),
        migrations.RunPython(bootstrap, migrations.RunPython.noop),
    ]
