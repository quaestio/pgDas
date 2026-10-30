from django.urls import path

from . import views

urlpatterns = [
    path("", views.audit_list, name="audit_list"),
    path("<int:pk>/json/", views.audit_json, name="audit_json"),
    path("<int:pk>/reinsert/", views.audit_reinsert, name="audit_reinsert"),
    path("<int:pk>/undo/", views.audit_undo, name="audit_undo"),
]
