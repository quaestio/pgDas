from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from access_control.models import Membership, DatabaseGrant


class EmployeeForm(forms.ModelForm):
    name = forms.CharField(max_length=150)
    email = forms.EmailField(max_length=150, label="Email", help_text="Used to sign in to PGDAS.")
    role = forms.ChoiceField(choices=Membership.Role.choices)
    password = forms.CharField(widget=forms.PasswordInput, required=False)
    password_confirm = forms.CharField(widget=forms.PasswordInput, required=False, label="Confirm password")

    class Meta:
        model = get_user_model()
        fields = ["name", "designation", "email", "role", "is_active", "password", "password_confirm"]
        labels = {"is_active": "Active"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields.pop("password")
            self.fields.pop("password_confirm")

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
        if not self.instance.pk and not password:
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
    organization = forms.CharField(max_length=150)
    installation_token = forms.CharField(widget=forms.PasswordInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("role")
        self.fields.pop("is_active")


class GrantForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=False, label="PostgreSQL execution password",
                               help_text="Leave blank to keep the existing password.")
    class Meta:
        model = DatabaseGrant
        fields = ["server", "database", "execution_username", "password", "read", "insert", "update", "delete", "truncate",
                  "create", "alter", "drop", "export", "backup_create", "backup_download"]
        labels = {"execution_username": "Restricted PostgreSQL login", "database": "Exact database name"}
