from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from access_control import mode, network, throttle
from access_control.models import DatabaseGrant, Membership, Organization


class EmailAuthenticationForm(AuthenticationForm):
    """The sign-in form: an email address and a password. (The field keeps the name "username" because Django's login view uses it.)"""
    username = forms.EmailField(label="Email", max_length=254, widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}))
    error_messages = {**AuthenticationForm.error_messages, "invalid_login": "Enter a correct email address and password."}

    def clean_username(self):
        return self.cleaned_data["username"].strip().lower()

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        from . import verification
        if verification.required(user):                      # the password was right, but this address was never proved to be theirs
            verification.send_link(self.request, user)
            self.unverified_user = user                      # the sign-in view sends them to the verification page
            raise ValidationError("Your email address is not verified yet.", code="unverified")

    def clean(self):
        email = self.cleaned_data.get("username")
        address = network.client_ip(self.request) if self.request is not None else None
        if email:
            wait = throttle.minutes_locked(email, str(address) if address else None)
            if wait:                                         # refused before the password is even looked at
                raise ValidationError(f"Too many failed sign-in attempts. Try again in {wait} minute{'' if wait == 1 else 's'}.", code="locked")
        try:
            cleaned = super().clean()
        except ValidationError as error:
            if email and error.code == "invalid_login":
                throttle.record_failure(email, str(address) if address else None)
            raise
        if email:
            throttle.clear(email)
        return cleaned


class EmployeeForm(forms.ModelForm):
    name = forms.CharField(max_length=150)
    email = forms.EmailField(max_length=150, label="Email", help_text="Used to sign in to PGDAS.")
    role = forms.ChoiceField(choices=Membership.Role.choices)
    password = forms.CharField(widget=forms.PasswordInput, required=False)
    password_confirm = forms.CharField(widget=forms.PasswordInput, required=False, label="Confirm password")
    invites = True                                     # with email configured, a new person is invited and chooses their own password

    class Meta:
        model = get_user_model()
        fields = ["name", "designation", "email", "role", "is_active", "password", "password_confirm"]
        labels = {"is_active": "Active"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inviting = bool(self.invites and not self.instance.pk and mode.can_send_email())
        if self.instance.pk or self.inviting:
            self.fields.pop("password")
            self.fields.pop("password_confirm")
        if self.inviting:
            self.fields.pop("is_active")
            self.fields["email"].help_text = "Used to sign in to PGDAS. A link to confirm it and choose a password is emailed here."
        self.waiting = bool(self.instance.pk and self.instance.email and not self.instance.is_active and not self.instance.has_usable_password())
        if self.waiting:
            self.fields["resend_invitation"] = forms.BooleanField(required=False, label="Send the invitation email again")

    def clean_email(self):
        from django.db.models import Q
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Another employee already uses this email address.")
        return email

    def clean(self):
        data = super().clean()
        email = data.get("email", "")
        password = data.get("password")
        if not self.instance.pk and not self.inviting and not password:
            self.add_error("password", "A password is required.")
        if password:
            if password != data.get("password_confirm"):
                self.add_error("password_confirm", "Passwords do not match.")
            candidate = get_user_model()(username=email, email=email, first_name=data.get("name", ""))
            try:
                validate_password(password, candidate)
            except ValidationError as error:
                self.add_error("password", error)
        return data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"]
        if commit:
            user.save()
        return user


class SetupForm(EmployeeForm):
    invites = False
    organization = forms.CharField(max_length=150)
    installation_token = forms.CharField(widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("role")
        self.fields.pop("is_active")
        self.fields["organization"].label = "Organization name"
        self.order_fields(["organization", "name", "designation", "email", "password", "password_confirm", "installation_token"])


class NewOrganizationForm(SetupForm):
    """Another organization, created together with its first Super User (an organization always has at least one)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("installation_token")
        self.fields["organization"].label = "Organization name"
        self.fields["name"].label = "First Super User: name"
        self.fields["email"].label = "First Super User: email"
        self.fields["designation"].label = "First Super User: designation"
        self.order_fields(["organization", "name", "designation", "email", "password", "password_confirm"])


class RegistrationForm(forms.Form):
    """The public page: a visitor registers an organization and becomes its first Super User. There is no password here: the person
    sets it from the link emailed to the address, which proves the address is theirs."""
    organization = forms.CharField(max_length=150, label="Organization name")
    name = forms.CharField(max_length=150, label="Your name")
    designation = forms.CharField(max_length=150, required=False, label="Your designation")
    email = forms.EmailField(max_length=150, label="Your email", help_text="Your sign-in ID. A link to confirm it is sent here.")
    accept_terms = forms.BooleanField(required=True, label="I have read and accept the Terms and Conditions",
                                      error_messages={"required": "You must accept the Terms and Conditions to register."})
    website = forms.CharField(required=False, label="", widget=forms.TextInput(attrs={"tabindex": "-1", "autocomplete": "off", "aria-hidden": "true",
                                                                                        "style": "position:absolute;left:-9999px;width:1px;height:1px"}))

    def clean_email(self):
        from django.db.models import Q
        email = self.cleaned_data["email"].strip().lower()
        if get_user_model().objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists():
            raise ValidationError("Another employee already uses this email address.")
        return email

    def clean_website(self):
        if self.cleaned_data.get("website"):                 # a field no person sees: only a script fills it in
            raise ValidationError("This registration was not accepted.")
        return ""


class OrganizationSettingsForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name", "restrict_creation", "allowed_ips"]
        labels = {"name": "Organization name", "restrict_creation": "Restrict organization creation",
                  "allowed_ips": "Allowed IP addresses (optional)"}
        help_texts = {
            "restrict_creation": "On: Super Users of this organization cannot create other organizations. Off: they can.",
            "allowed_ips": "One address or range per line, for example 203.0.113.7 or 10.0.0.0/24 (IPv4 or IPv6). Leave empty for no "
                           "restriction. When set, everyone except Super Users can use PGDAS only from these addresses; Super Users can "
                           "always sign in from any address.",
        }
        widgets = {"allowed_ips": forms.Textarea(attrs={"rows": 5, "spellcheck": "false"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not mode.is_shared():                             # one organization only: there is nothing to restrict
            self.fields.pop("restrict_creation")

    def clean_allowed_ips(self):
        text = self.cleaned_data.get("allowed_ips", "")
        networks, bad = network.parse_networks(text)
        if bad:
            raise ValidationError("These are not valid addresses or ranges: " + ", ".join(bad))
        return network.normalise(text)


class GrantForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=False, label="PostgreSQL execution password",
                               help_text="Leave blank to keep the existing password.")
    class Meta:
        model = DatabaseGrant
        fields = ["server", "database", "execution_username", "password", "read", "insert", "update", "delete", "truncate",
                  "create", "alter", "drop", "export", "backup_create", "backup_download",
                  "run_sql", "maintenance", "views", "sequences", "schemas", "functions", "triggers", "extensions"]
        labels = {"execution_username": "Restricted PostgreSQL login", "database": "Exact database name",
                  "run_sql": "Run own SQL", "maintenance": "Maintenance (vacuum, analyze, reindex)", "views": "Views", "sequences": "Sequences", "schemas": "Schemas",
                  "functions": "Functions (needs a restricted login)", "triggers": "Triggers (needs a restricted login)",
                  "extensions": "Extensions (needs a restricted login)"}
