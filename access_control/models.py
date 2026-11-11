"""Permission storage, independent of the eventual custom employee model."""
from django.conf import settings
from django.db import models
from django.utils import timezone


class Organization(models.Model):
    name = models.CharField(max_length=150)
    active = models.BooleanField(default=True)
    # Whether this organization's Super Users may create other organizations. On by default: creation never spreads by accident.
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


class SignInFailure(models.Model):
    """One failed sign-in: the email address that was tried (whether or not it exists) and the address it came from. See throttle.py."""
    email = models.CharField(max_length=254, db_index=True)
    address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)


class Installation(models.Model):
    completed = models.BooleanField(default=False)
