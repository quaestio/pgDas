from django.urls import path

from . import views

urlpatterns = [
    path("", views.watch_page, name="watch"),
    path("data/", views.watch_data, name="watch_data"),
    path("sample/", views.watch_sample, name="watch_sample"),
    path("test-notify/", views.watch_test_notify, name="watch_test_notify"),
]
