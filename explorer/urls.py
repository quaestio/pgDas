from django.urls import path

from . import views

urlpatterns = [
    path("c/<sid:pk>/sql/export/", views.sql_export, name="sql_export"),
    path("browse/tree/", views.tree_nodes, name="tree"),
    path("browse/detail/", views.detail, name="detail"),
    path("c/<sid:pk>/", views.explorer, name="explorer"),
    path("c/<sid:pk>/sql/", views.sql_runner, name="sql_runner"),
    path("c/<sid:pk>/sql/run/", views.sql_run, name="sql_run"),
    path("c/<sid:pk>/sql/page/", views.sql_page, name="sql_page"),
    path("c/<sid:pk>/sql/saved/", views.sql_saved, name="sql_saved"),
    path("c/<sid:pk>/sql/saved/<int:query_id>/delete/", views.sql_saved_delete, name="sql_saved_delete"),
    path("c/<sid:pk>/sql/write/preview/", views.write_preview, name="write_preview"),
    path("c/<sid:pk>/sql/write/", views.write_execute, name="write_execute"),
    path("c/<sid:pk>/rows/preview/", views.rows_preview, name="rows_preview"),
    path("c/<sid:pk>/rows/", views.rows_execute, name="rows_execute"),
    path("c/<sid:pk>/table/export/", views.table_export, name="table_export"),
    path("c/<sid:pk>/maintenance/", views.maintenance_run, name="maintenance_run"),
    path("c/<sid:pk>/types/", views.column_types, name="column_types"),
    path("c/<sid:pk>/tables/", views.database_tables, name="database_tables"),
]
