from django.urls import path

from . import views

urlpatterns = [
    path("<int:pk>/", views.explorer, name="explorer"),
    path("<int:pk>/tree/", views.tree_nodes, name="tree"),
    path("<int:pk>/detail/", views.detail, name="detail"),
    path("<int:pk>/sql/", views.sql_runner, name="sql_runner"),
]
