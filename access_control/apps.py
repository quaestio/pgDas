from django.apps import AppConfig


class AccessControlConfig(AppConfig):
    name = "access_control"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system check)