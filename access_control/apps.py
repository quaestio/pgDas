from django.apps import AppConfig


class AccessControlConfig(AppConfig):
    name = "access_control"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401  (registers the system check)
        from django.contrib.auth.signals import user_logged_in
        from . import signals
        user_logged_in.connect(signals.follow_new_databases, dispatch_uid="pgdas-follow-new-databases")