from django import forms

from . import netpolicy
from .models import ServerConnection


class ServerForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False,
                               help_text="Leave blank to keep the saved password.")

    class Meta:
        model = ServerConnection
        # No Database field: PGDAS picks a database this login can open (see explorer.pg.connect),
        # and the tree lists only the databases the login is allowed to connect to.
        fields = ["name", "host", "port", "username", "password", "sslmode"]

    def clean_host(self):
        host = self.cleaned_data["host"]
        try:
            netpolicy.clean_host(host)
            if netpolicy.policy() == "public":
                try:
                    netpolicy.check(host)
                except netpolicy.AddressNotAllowed as error:
                    if netpolicy.resolve(host):              # it resolves, and the answer is not allowed; a name that does not resolve yet is accepted
                        raise error
        except netpolicy.AddressNotAllowed as error:
            raise forms.ValidationError(str(error))
        return host.strip()

    def clean(self):
        data = super().clean()
        if not self.instance.pk and not data.get("password"):
            self.add_error("password", "Enter the password for this server.")
        return data

    def save(self, commit=True):
        server = super().save(commit=False)
        password = self.cleaned_data.get("password")
        if password:
            server.set_password(password)
        if commit:
            server.save()
        return server
