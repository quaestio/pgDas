"""The Terms and Conditions a registering organization accepts. The built-in text (templates/accounts/terms.html) is a starting
point; an operator who has terms of their own sets PGDAS_TERMS_URL and registrations link to those instead."""
from django.conf import settings
from django.urls import reverse

VERSION = "2026-11-10"          # change when templates/accounts/terms.html changes; the version a person accepted is recorded


def url():
    return settings.TERMS_URL or reverse("terms")