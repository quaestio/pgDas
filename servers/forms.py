from django import forms

from .models import ServerConnection


class ServerForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False,
                               help_text="Leave blank to keep the saved password.")

    class Meta:
        model = ServerConnection
        # No Database field: PgDeck picks a database this login can open (see explorer.pg.connect),
        # and the tree lists only the databases the login is allowed to connect to.
        fields = ["name", "host", "port", "username", "password", "sslmode"]

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
