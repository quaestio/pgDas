"""PGDAS_BEHIND_HTTPS_PROXY: links built from a request that came through an HTTPS-terminating proxy use https."""
from django.core import mail
from django.test import Client, TestCase, override_settings

from access_control.models import Installation, Membership, Organization
from django.contrib.auth import get_user_model
from django.urls import reverse

EMAIL = dict(EMAIL_HOST="smtp.test", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


@override_settings(MODE="shared", ALLOW_ORGANIZATION_REGISTRATION=True, **EMAIL)
class ProxyHttpsTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        Membership.objects.create(user=get_user_model().objects.create_user("a", email="a@o.test", password="x"), organization=Organization.objects.create(name="O"), role="super_user")

    def register(self, name):
        mail.outbox = []
        Client().post(reverse("register"), {"organization": name, "name": "B", "designation": "", "email": f"{name}@n.test", "accept_terms": "on", "website": ""},
                      HTTP_X_FORWARDED_PROTO="https", HTTP_HOST="testserver")
        return mail.outbox[-1].body

    def test_the_header_is_believed_only_when_the_setting_is_on(self):
        with override_settings(SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https")):
            self.assertIn("https://testserver/register/confirm/", self.register("one"))
        with override_settings(SECURE_PROXY_SSL_HEADER=None):
            self.assertIn("http://testserver/register/confirm/", self.register("two"))

    def test_the_setting_is_off_by_default(self):
        from django.conf import settings
        self.assertFalse(settings.BEHIND_HTTPS_PROXY)