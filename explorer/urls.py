from django.urls import path

from . import views

urlpatterns = [
    path("browse/tree/", views.tree_nodes, name="tree"),
    path("browse/detail/", views.detail, name="detail"),
    path("c/<int:pk>/", views.explorer, name="explorer"),
    path("c/<int:pk>/sql/", views.sql_runner, name="sql_runner"),
    path("c/<int:pk>/sql/delete/preview/", views.delete_preview, name="delete_preview"),
    path("c/<int:pk>/sql/delete/", views.delete_execute, name="delete_execute"),
]
