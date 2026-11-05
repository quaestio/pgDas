from django.contrib.auth import views as auth_views
from django.urls import include, path

urlpatterns = [
    path("", include("accounts.urls")),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("audit/", include("audit.urls")),
    path("backups/", include("backups.urls")),
    path("monitor/", include("monitor.urls")),
    path("watch/", include("watch.urls")),
    path("dashboard/", include("dashboard.urls")),
    path("", include("explorer.urls")),
    path("", include("servers.urls")),
]
