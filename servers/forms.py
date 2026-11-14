from django import forms

from . import netpolicy
from .models import ServerConnection


class ServerForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False,
                               help_text="Leave blank to keep the saved password.")
    # "1" (or nothing) = the password is saved; "0" = it is not: a Super User unlocks the server with it for a few hours (see unlock.py).
    save_password = forms.ChoiceField(choices=[("1", "Save the password"), ("0", "Do not save it (a Super User enters it to unlock the server)")],
                                      required=False, initial="1", label="Save password")
    # SSH tunnel (all optional): reach the server through an SSH server instead of directly.
    ssh_password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False, label="SSH password",
                                   help_text="Leave blank to keep the saved one.")
    ssh_key = forms.CharField(widget=forms.Textarea(attrs={"rows": 6, "spellcheck": "false", "autocomplete": "off",
                                                           "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----"}),
                              required=False, strip=False, label="SSH private key", help_text="Paste the whole key. Leave blank to keep the saved one.")
    ssh_passphrase = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False, strip=False, label="Key passphrase",
                                     help_text="Only if the key is protected by one.")

    class Meta:
        model = ServerConnection
        # No Database field: PGDAS picks a database this login can open (see explorer.pg.connect),
        # and the tree lists only the databases the login is allowed to connect to.
        fields = ["name", "comments", "host", "port", "database", "username", "password", "kerberos", "krbsrvname", "sslmode", "ssh_enabled", "ssh_host", "ssh_port", "ssh_username", "ssh_auth", "ssh_prompt_passphrase"]
        labels = {"database": "Database to connect to first", "kerberos": "Kerberos authentication", "krbsrvname": "Kerberos service name",
                  "ssh_prompt_passphrase": "Ask for the passphrase (do not store it)", "ssh_enabled": "Connect through an SSH tunnel", "ssh_host": "SSH host", "ssh_port": "SSH port", "ssh_username": "SSH user name",
                  "ssh_auth": "SSH sign-in with"}
        help_texts = {"database": "Optional. Leave empty and PGDAS picks a database the login can open.",
                      "krbsrvname": "Only if PostgreSQL's Kerberos service is not called postgres.", "host": "With an SSH tunnel: as the SSH server sees it (for example 127.0.0.1 if PostgreSQL runs on the SSH server)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("ssh_host", "ssh_port", "ssh_username", "ssh_auth", "ssh_prompt_passphrase", "database", "kerberos", "krbsrvname", "comments"):
            self.fields[name].required = False                  # only needed when the tunnel is switched on (see clean)
        if self.instance.pk:
            self.initial["save_password"] = "1" if self.instance.save_password else "0"

    def _host_rule(self, field, host):
        """The address rule of this installation for a host that PGDAS itself connects to."""
        try:
            netpolicy.clean_host(host)
            if netpolicy.policy() == "public":
                try:
                    netpolicy.check(host)
                except netpolicy.AddressNotAllowed as error:
                    if netpolicy.resolve(host):              # it resolves, and the answer is not allowed; a name that does not resolve yet is accepted
                        raise error
        except netpolicy.AddressNotAllowed as error:
            self.add_error(field, str(error))

    def clean_host(self):
        host = self.cleaned_data["host"]
        try:
            netpolicy.clean_host(host)                       # its shape; the address rule is in clean(): it depends on whether an SSH server is used
        except netpolicy.AddressNotAllowed as error:
            raise forms.ValidationError(str(error))
        return host.strip()

    def clean_save_password(self):
        return self.cleaned_data.get("save_password") != "0"                     # not sent at all counts as "save"

    def clean(self):
        data = super().clean()
        data["save_password"] = data.get("save_password", True) is not False
        if data.get("kerberos"):
            from django.conf import settings
            if not settings.KERBEROS:
                self.add_error("kerberos", "Kerberos sign-in is not switched on for this installation (the person who runs PGDAS sets PGDAS_KERBEROS=1 in .env).")
        elif not self.instance.pk and not data.get("password"):
            self.add_error("password", "Enter the password for this server." if data.get("save_password") is not False else
                           "Enter the password once: it is used for the test and to unlock the server, and is not saved.")
        host = data.get("host")
        if data.get("ssh_enabled"):
            # The PostgreSQL host is looked up by the SSH server, so it may be a private address there; the SSH server is what PGDAS reaches.
            ssh_host = (data.get("ssh_host") or "").strip()
            if not ssh_host:
                self.add_error("ssh_host", "Enter the SSH host.")
            else:
                self._host_rule("ssh_host", ssh_host)
                data["ssh_host"] = ssh_host
            if not (data.get("ssh_username") or "").strip():
                self.add_error("ssh_username", "Enter the SSH user name.")
            auth = data.get("ssh_auth") or "password"
            saved_same = self.instance.pk and self.instance.ssh_enabled and self.instance.ssh_auth == auth and self.instance.ssh_secret_encrypted
            if auth == "key" and not data.get("ssh_key") and not saved_same:
                self.add_error("ssh_key", "Paste the SSH private key.")
            if auth == "password" and not data.get("ssh_password") and not saved_same:
                self.add_error("ssh_password", "Enter the SSH password.")
            if data.get("ssh_prompt_passphrase"):
                if auth != "key":
                    self.add_error("ssh_prompt_passphrase", "Asking for a passphrase applies to a private key only.")
                elif not data.get("ssh_passphrase") and not self.instance.pk:
                    self.add_error("ssh_passphrase", "Enter the passphrase once: it is used for the test and to unlock the server, and is not stored.")
        elif host:
            self._host_rule("host", host)
        return data

    def save(self, commit=True):
        server = super().save(commit=False)
        password = self.cleaned_data.get("password")
        server.save_password = self.cleaned_data.get("save_password") is not False
        if server.kerberos:
            server.password_encrypted = ""                       # Kerberos: there is no password
        elif not server.save_password:
            server.password_encrypted = ""                       # not kept: the Super User unlocks the server instead
            if password:
                server._typed_password = password                # only for the test right now
        elif password:
            server.set_password(password)
        if server.ssh_enabled:
            if server.ssh_auth == "key" and self.cleaned_data.get("ssh_key"):
                server.set_ssh_secret(self.cleaned_data["ssh_key"].strip() + "\n")
            elif server.ssh_auth == "password" and self.cleaned_data.get("ssh_password"):
                server.set_ssh_secret(self.cleaned_data["ssh_password"])
            typed = self.cleaned_data.get("ssh_passphrase")
            if server.ssh_prompt_passphrase and server.ssh_auth == "key":
                server.set_ssh_passphrase("")                # not kept: a Super User unlocks the server instead (see unlock.py)
                if typed is not None and typed != "":
                    server._typed_ssh_passphrase = typed     # only for the test right now
            elif typed:
                server.set_ssh_passphrase(typed)
            elif server.ssh_auth == "password":
                server.set_ssh_passphrase("")
            if "ssh_host" in self.changed_data or "ssh_port" in self.changed_data:
                server.ssh_host_key = ""                     # another SSH server: its key is learned again
        if commit:
            server.save()
        return server