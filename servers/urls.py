from django.urls import path

from . import views

urlpatterns = [
    path("", views.server_list, name="server_list"),
    path("servers/add/", views.server_add, name="server_add"),
    path("servers/test-draft/", views.server_test_draft, name="server_test_draft"),
    path("servers/create/", views.server_create, name="server_create"),
    path("servers/<sid:pk>/edit/", views.server_edit, name="server_edit"),
    path("servers/<sid:pk>/json/", views.server_json, name="server_json"),
    path("servers/<sid:pk>/update/", views.server_update, name="server_update"),
    path("servers/<sid:pk>/delete/", views.server_delete, name="server_delete"),
    path("servers/<sid:pk>/test/", views.server_test, name="server_test"),
    path("servers/<sid:pk>/unlock/", views.server_unlock, name="server_unlock"),
    path("servers/<sid:pk>/lock/", views.server_lock, name="server_lock"),
]
