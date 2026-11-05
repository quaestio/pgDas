from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("access_control", "0003_alter_installation_id")]
    operations = [
        migrations.AddField("organization", "restrict_creation", models.BooleanField(default=True)),
        migrations.AddField("organization", "allowed_ips", models.TextField(blank=True)),
    ]