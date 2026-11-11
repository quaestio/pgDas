from django.contrib.auth.models import AbstractUser, Group, Permission
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class User(AbstractUser):
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    designation = models.CharField(max_length=150, blank=True)
    email_verified = models.BooleanField(default=False)          # set when the person opens the emailed link (registration, invitation or verification)
    # Preserve existing primary keys, password hashes and foreign keys during upgrade.
    groups = models.ManyToManyField(Group, blank=True, related_name="pgdas_employees")
    user_permissions = models.ManyToManyField(Permission, blank=True, related_name="pgdas_employees")

    class Meta:
        db_table = "auth_user"
        constraints = [
            # The email address is the sign-in ID: unique across the whole installation, whatever the capital letters.
            # (An account that has no address yet is allowed; it cannot sign in until a Super User gives it one.)
            models.UniqueConstraint(Lower("email"), condition=~Q(email=""), name="accounts_user_email_unique_ci"),
        ]

    def save(self, *args, **kwargs):
        """Email is the sign-in ID: kept lower case and trimmed, and copied into the (internal) username."""
        self.email = (self.email or "").strip().lower()
        if self.email:
            self.username = self.email
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "email" in update_fields and "username" not in update_fields:
                kwargs["update_fields"] = [*update_fields, "username"]
        super().save(*args, **kwargs)

    @property
    def display_name(self):
        return self.get_full_name() or self.username

