from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard_page, name="dashboard"),
    path("data/", views.dashboard_data, name="dashboard_data"),
]
