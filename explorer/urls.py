from django.urls import path

from . import views

urlpatterns = [
    path("browse/tree/", views.tree_nodes, name="tree"),
    path("browse/detail/", views.detail, name="detail"),
    path("c/<int:pk>/", views.explorer, name="explorer"),
    path("c/<int:pk>/sql/", views.sql_runner, name="sql_runner"),
    path("c/<int:pk>/sql/write/preview/", views.write_preview, name="write_preview"),
    path("c/<int:pk>/sql/write/", views.write_execute, name="write_execute"),
    path("c/<int:pk>/rows/preview/", views.rows_preview, name="rows_preview"),
    path("c/<int:pk>/rows/", views.rows_execute, name="rows_execute"),
]
