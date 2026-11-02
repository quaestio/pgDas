from django.urls import path

from . import views

urlpatterns = [
    path("data/", views.dashboard_data, name="dashboard_data"),
]
