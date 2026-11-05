from django.contrib.auth.models import AbstractUser, Group, Permission
from django.db import models


class User(AbstractUser):
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)
    designation = models.CharField(max_length=150, blank=True)
    # Preserve existing primary keys, password hashes and foreign keys during upgrade.
    groups = models.ManyToManyField(Group, blank=True, related_name="pgdas_employees")
    user_permissions = models.ManyToManyField(Permission, blank=True, related_name="pgdas_employees")

    class Meta:
        db_table = "auth_user"

    @property
    def display_name(self):
        return self.get_full_name() or self.username

