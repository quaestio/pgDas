from django.urls import path

from . import views

urlpatterns = [path("", views.audit_list, name="audit_list")]
