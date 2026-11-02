from django.urls import path

from . import views

urlpatterns = [
    path("", views.monitor_page, name="monitor"),
    path("data/", views.monitor_data, name="monitor_data"),
    path("stop/", views.monitor_stop, name="monitor_stop"),
]
