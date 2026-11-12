from django.urls import path
from django.contrib.auth import views as auth_views
from . import profile as profile_views, registration, views, invitations, verification

urlpatterns = [
    path("setup/", views.setup, name="setup"),
    path("register/", registration.register, name="register"),
    path("terms/", views.terms, name="terms"),
    path("register/confirm/<str:token>/", registration.confirm, name="register_confirm"),
    path("verify-email/", verification.pending, name="email_verify_pending"),
    path("verify-email/<str:token>/", verification.verify, name="email_verify"),
    path("invitation/<str:token>/", invitations.accept, name="invitation_accept"),
    path("employees/", views.employees, name="employee_list"),
    path("employees/add/", views.employee_edit, name="employee_add"),
    path("employees/<int:pk>/edit/", views.employee_edit, name="employee_edit"),
    path("employees/<int:pk>/delete/", views.employee_delete, name="employee_delete"),
    path("employees/<int:pk>/permissions/", views.permissions, name="employee_permissions"),
    path("employees/<int:pk>/permissions/<int:grant_pk>/revoke/", views.revoke, name="employee_revoke"),
    path("organization/", views.organization_edit, name="organization_edit"),
    path("organization/new/", views.organization_create, name="organization_create"),
    path("profile/", profile_views.profile, name="profile"),
    path("password/", auth_views.PasswordChangeView.as_view(template_name="accounts/form.html", success_url="/", extra_context={"title": "Change password"}), name="password_change"),
]
