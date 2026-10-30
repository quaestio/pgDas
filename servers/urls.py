from django.urls import path

from . import views

urlpatterns = [
    path("", views.server_list, name="server_list"),
    path("servers/add/", views.server_add, name="server_add"),
    path("servers/<int:pk>/edit/", views.server_edit, name="server_edit"),
    path("servers/<int:pk>/delete/", views.server_delete, name="server_delete"),
    path("servers/<int:pk>/test/", views.server_test, name="server_test"),
]
