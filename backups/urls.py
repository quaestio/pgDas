from django.urls import path

from . import views

urlpatterns = [
    path("", views.backup_list, name="backup_list"),
    path("databases/", views.database_choices, name="backup_databases"),
    path("create/", views.backup_create, name="backup_create"),
    path("<int:pk>/download/", views.backup_download, name="backup_download"),
    path("<int:pk>/delete/", views.backup_delete, name="backup_delete"),
    path("<int:pk>/restore/", views.backup_restore, name="backup_restore"),
    path("schedule/", views.schedule_save, name="schedule_save"),
    path("schedule/<int:pk>/delete/", views.schedule_delete, name="schedule_delete"),
]
