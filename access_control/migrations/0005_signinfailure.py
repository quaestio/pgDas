import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("access_control", "0004_organization_settings")]
    operations = [
        migrations.CreateModel(
            name="SignInFailure",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.CharField(db_index=True, max_length=254)),
                ("address", models.GenericIPAddressField(blank=True, db_index=True, null=True)),
                ("at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
            ],
        ),
    ]