"""Test base class for the suites written before PGDAS had organizations and roles.

PGDAS now refuses every request until the installation is complete, and answers only people who belong to an organization
with a role. These tests were about what the pages do, not about access control (access_control and accounts test that), so
this TestCase sets the stage once and leaves the tests alone:

  * the installation is complete and there is one test organization;
  * every user a test creates joins it: staff users as Super Users, everyone else as a Viewer without any database grant;
  * every saved server a test creates belongs to it.

Tests that are about permissions use django.test.TestCase and build their own people, as access_control and accounts do.
"""
from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.test import TestCase as DjangoTestCase

from access_control.models import Installation, Membership, Organization, OrganizationServer
from servers.models import ServerConnection


def organization():
    return Organization.objects.get_or_create(name="Test organization")[0]


def _user_saved(sender, instance, created, **kwargs):
    """The old tests flip `user.is_staff` and save; the role follows it, as it did when staff was the only distinction."""
    role = "super_user" if instance.is_staff else "viewer"
    member = Membership.objects.filter(user=instance).first()
    if member is None:
        Membership.objects.create(user=instance, organization=organization(), role=role)
    elif member.role in ("super_user", "viewer") and member.role != role:
        member.role = role
        member.save(update_fields=["role"])


def _server_saved(sender, instance, created, **kwargs):
    if created:
        OrganizationServer.objects.get_or_create(server=instance, defaults={"organization": organization()})


class TestCase(DjangoTestCase):
    @classmethod
    def setUpClass(cls):
        post_save.connect(_user_saved, sender=get_user_model(), dispatch_uid="pgdas_test_user")
        post_save.connect(_server_saved, sender=ServerConnection, dispatch_uid="pgdas_test_server")
        super().setUpClass()
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            post_save.disconnect(dispatch_uid="pgdas_test_user", sender=get_user_model())
            post_save.disconnect(dispatch_uid="pgdas_test_server", sender=ServerConnection)
