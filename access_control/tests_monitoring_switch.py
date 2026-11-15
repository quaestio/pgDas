"""The operator's switch for one organization's monitoring."""
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from access_control.models import Organization


class MonitoringSwitchTests(TestCase):
    def test_it_switches_one_organization_and_the_list_shows_it(self):
        Organization.objects.create(name="Loud")
        call_command("organizations", "--monitoring", "off", "Loud", stdout=StringIO())
        self.assertFalse(Organization.objects.get(name="Loud").monitoring_enabled)
        listing = StringIO()
        call_command("organizations", stdout=listing)
        self.assertRegex(listing.getvalue(), r"OFF\s+\d+\s+\d+\s+\d+\s+Loud")
        call_command("organizations", "--monitoring", "on", "Loud", stdout=StringIO())
        self.assertTrue(Organization.objects.get(name="Loud").monitoring_enabled)

    def test_bad_input_is_explained(self):
        with self.assertRaisesMessage(CommandError, "on or off"):
            call_command("organizations", "--monitoring", "maybe", "X", stdout=StringIO())
        with self.assertRaisesMessage(CommandError, "no organization named"):
            call_command("organizations", "--monitoring", "off", "Nobody", stdout=StringIO())