from django.urls import path

from . import views

urlpatterns = [
    path("features/", views.monitor_features, name="monitor_features"),
    path("features/change/", views.monitor_feature_change, name="monitor_feature_change"),
    path("", views.monitor_page, name="monitor"),
    path("data/", views.monitor_data, name="monitor_data"),
    path("stop/", views.monitor_stop, name="monitor_stop"),
]
