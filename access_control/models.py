"""Permission storage, independent of the eventual custom employee model."""
from django.conf import settings
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class Organization(models.Model):
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    # Whether this organization's Super Users may create other organizations. On by default: creation never spreads by accident.
    # Free monitoring for everybody on a shared installation; the person who runs PGDAS can switch it off for one organization
    # (manage.py organizations --monitoring off NAME), for example one that sends too much load.
    monitoring_enabled = models.BooleanField(default=True)
    # Where this organization's alerts go (see watch/notify.py). The webhook address is a secret: only its host is ever shown. Recipients are in AlertRecipient.
    alert_webhook_url = models.CharField(max_length=500, blank=True)
    restrict_creation = models.BooleanField(default=True)
    # Addresses (one per line; a single address or a range such as 10.0.0.0/24) from which everyone except Super Users may use PGDAS.
    # Empty = no restriction. Checked on every request after sign-in.
    allowed_ips = models.TextField(blank=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    class Role(models.TextChoices):
        SUPER_USER = "super_user", "Super User"
        EDITOR = "editor", "Editor"
        VIEWER = "viewer", "Viewer"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pgdas_membership")
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.VIEWER)


class OrganizationServer(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    server = models.OneToOneField("servers.ServerConnection", on_delete=models.CASCADE, related_name="organization_scope")


class DatabaseGrant(models.Model):
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="database_grants")
    server = models.ForeignKey("servers.ServerConnection", on_delete=models.CASCADE)
    # PostgreSQL identifiers are case-sensitive when quoted. Never lowercase or normalize this value.
    database = models.CharField(max_length=63)
    read = models.BooleanField(default=True)
    insert = models.BooleanField(default=False)
    update = models.BooleanField(default=False)
    delete = models.BooleanField(default=False)
    truncate = models.BooleanField(default=False)
    create = models.BooleanField(default=False)
    alter = models.BooleanField(default=False)
    drop = models.BooleanField(default=False)
    export = models.BooleanField(default=False)
    backup_create = models.BooleanField(default=False)
    backup_download = models.BooleanField(default=False)
    run_sql = models.BooleanField(default=False)               # may type and run their own SQL
    maintenance = models.BooleanField(default=False)           # VACUUM, ANALYZE, REINDEX on tables of this database
    views = models.BooleanField(default=False)                 # create / drop views and materialized views
    sequences = models.BooleanField(default=False)
    schemas = models.BooleanField(default=False)
    # Function bodies, triggers and extensions run with the login's own power and PGDAS cannot read what they do, so these three only
    # take effect on an assignment that has its own restricted PostgreSQL login.
    functions = models.BooleanField(default=False)
    triggers = models.BooleanField(default=False)
    extensions = models.BooleanField(default=False)
    execution_username = models.CharField(max_length=100, blank=True)
    execution_password = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["membership", "server", "database"], name="unique_employee_database_grant")]

    def clean(self):
        from django.core.exceptions import ValidationError
        scope = OrganizationServer.objects.filter(server_id=self.server_id).first()
        if not scope or scope.organization_id != self.membership.organization_id:
            raise ValidationError("The employee and server must belong to the same organization.")
        if not self.database or "\x00" in self.database or len(self.database.encode("utf-8")) > 63:
            raise ValidationError("Use an exact PostgreSQL database name of at most 63 UTF-8 bytes.")


class AlertRecipient(models.Model):
    """An email address that receives an organization's alerts. It receives nothing until its owner has confirmed it (a link is emailed to it), so nobody
    can use PGDAS to send alerts to an address that is not theirs; every alert email also carries a link to stop."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="alert_recipients")
    email = models.CharField(max_length=254)
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    verification_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), "organization", name="unique_alert_recipient_per_organization")]
        ordering = ["email"]

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)


class ServerWideAccess(models.Model):
    """"Every database on this server at this level", including databases created later: the grants are kept in step (see serverwide.py).
    A database the Super User set apart is listed in `excluded` and is left alone."""
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name="server_wide")
    server = models.ForeignKey("servers.ServerConnection", on_delete=models.CASCADE)
    level = models.CharField(max_length=20)
    run_sql = models.BooleanField(default=False)
    excluded = models.JSONField(default=list, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["membership", "server"], name="unique_server_wide_access")]


class SignInFailure(models.Model):
    """One failed sign-in: the email address that was tried (whether or not it exists) and the address it came from. See throttle.py."""
    email = models.CharField(max_length=254, db_index=True)
    address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)


class Installation(models.Model):
    completed = models.BooleanField(default=False)
