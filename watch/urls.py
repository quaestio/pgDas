from django.urls import path

from . import views

urlpatterns = [
    path("", views.watch_page, name="watch"),
    path("data/", views.watch_data, name="watch_data"),
    path("dashboards/", views.dashboards_page, name="dashboards"),
    path("dashboards/data/", views.dashboards_data, name="dashboards_data"),
    path("dashboards/live/", views.dashboards_live, name="dashboards_live"),
    path("dashboards/settings/preview/", views.setting_preview, name="setting_preview"),
    path("dashboards/settings/", views.setting_apply, name="setting_apply"),
    path("sample/", views.watch_sample, name="watch_sample"),
    path("test-notify/", views.watch_test_notify, name="watch_test_notify"),
    path("health/", views.health_page, name="health"),
    path("health/databases/", views.health_databases, name="health_databases"),
    path("health/data/", views.health_data, name="health_data"),
    path("health/measure/", views.health_bloat, name="health_bloat"),
    path("alerts/", views.alert_settings, name="alert_settings"),
    path("alerts/confirm/<str:token>/", views.alert_confirm, name="alert_confirm"),
    path("alerts/stop/<str:token>/", views.alert_stop, name="alert_stop"),
]
