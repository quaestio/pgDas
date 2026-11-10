from django.contrib.auth import views as auth_views

from access_control import mode

from . import terms
from .forms import EmailAuthenticationForm, RegistrationForm


class LoginView(auth_views.LoginView):
    template_name = "registration/login.html"
    authentication_form = EmailAuthenticationForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["registration_open"] = mode.registration_open()          # offer "Register your organization" only when it works
        if context["registration_open"]:
            context["register_form"] = RegistrationForm()
            context["terms_url"] = terms.url()
        return context