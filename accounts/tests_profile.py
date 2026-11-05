"""The person menu in the header (avatar, name, designation; Profile, Change Password, Logout) and the Profile page."""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from access_control.models import Installation, Membership, Organization
from audit.models import AuditEntry

BASE_DIR = Path(settings.BASE_DIR)


def initials(text):
    return Template("{% load people %}{{ value|initials }}").render(Context({"value": text}))


class InitialsTests(TestCase):
    def test_the_avatar_letters(self):
        for text, expected in (("Soumen Das", "SD"), ("Ana Maria Silva", "AS"), ("single", "S"), ("mail.soumen@gmail.com", "MS"),
                               ("ops@example.test", "O"), ("  ", "?"), ("", "?"), (None, "?"), ("soumen das", "SD")):
            with self.subTest(text=text):
                self.assertEqual(initials(text), expected)


class PersonMenuTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Quaestio")
        self.user = get_user_model().objects.create_user("mail.soumen@gmail.com", email="mail.soumen@gmail.com", password="pw-12345-abc",
                                                         first_name="Soumen Das", designation="Database Administrator")
        Membership.objects.create(user=self.user, organization=self.org, role="super_user")
        self.client.force_login(self.user)

    def header(self, path="/"):
        html = self.client.get(path).content.decode()
        return html, html[html.index("<header>"):html.index("</header>")]

    def test_the_header_shows_avatar_name_and_designation_and_not_the_email(self):
        html, header = self.header()
        trigger = header[header.index('id="userMenuButton"'):header.index("</button>", header.index('id="userMenuButton"'))]
        self.assertIn('<span class="avatar" aria-hidden="true">SD</span>', trigger)
        self.assertIn("<strong>Soumen Das</strong>", trigger)
        self.assertIn("<small>Database Administrator</small>", trigger)
        self.assertNotIn("mail.soumen@gmail.com", header)                                  # the email is not in the header any more
        self.assertNotIn('class="who"', html)

    def test_without_a_name_the_email_still_gives_letters_and_without_a_designation_there_is_no_second_line(self):
        self.user.first_name, self.user.designation = "", ""
        self.user.save()
        _, header = self.header()
        self.assertIn('class="avatar" aria-hidden="true">MS<', header)
        self.assertNotIn("<small>", header[header.index('id="userMenuButton"'):header.index('id="userMenuPanel"')])

    def test_the_menu_has_profile_change_password_and_logout_in_that_order_and_no_sign_out_button(self):
        html, header = self.header()
        panel = header[header.index('id="userMenuPanel"'):]
        order = [panel.index(text) for text in ('href="/profile/"', 'href="/password/"', 'action="/logout/"')]
        self.assertEqual(order, sorted(order))
        for label in ("Profile", "Change Password", "Logout"):
            self.assertIn(f">{label}</", panel)
        self.assertEqual(panel.count('role="menuitem"'), 3)
        self.assertIn('method="post" action="/logout/"', panel)                            # logging out is a POST, with the CSRF token
        self.assertIn("csrfmiddlewaretoken", panel)
        self.assertNotIn("Sign out", html)
        self.assertIn("hidden>", panel[:panel.index(">") + 20] + ">")                       # closed until it is opened

    def test_the_menu_is_there_on_every_page_and_for_every_role(self):
        for path in ("/", "/profile/", "/backups/", "/audit/"):
            with self.subTest(path=path):
                self.assertIn('id="userMenuButton"', self.client.get(path).content.decode())
        viewer = get_user_model().objects.create_user("viewer@example.test", password="pw-12345-abc", first_name="Vera Viewer")
        Membership.objects.create(user=viewer, organization=self.org, role="viewer")
        self.client.force_login(viewer)
        _, header = self.header()
        self.assertIn("<strong>Vera Viewer</strong>", header)
        self.assertIn('>VV<', header)

    def test_logout_from_the_menu_ends_the_session(self):
        response = self.client.post(reverse("logout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/profile/").status_code, 302)                    # signed out: back to the sign-in page

    def test_script_and_styles_are_linked_and_make_a_real_dropdown(self):
        html, _ = self.header()
        self.assertIn('href="/static/usermenu.css"', html)
        self.assertIn('src="/static/usermenu.js"', html)
        script = (BASE_DIR / "static" / "usermenu.js").read_text(encoding="utf-8")
        for needle in ("aria-expanded", '"Escape"', "ArrowDown", "ArrowUp", "wrap.contains(event.target)",
                       "event.stopPropagation()"):                                            # ArrowDown on the button must land on the FIRST item
            self.assertIn(needle, script)
        css = (BASE_DIR / "static" / "usermenu.css").read_text(encoding="utf-8")
        for needle in (".user-panel {", ".user-trigger {", ".avatar.big", "prefers-reduced-motion"):
            self.assertIn(needle, css)


class ProfilePageTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        org = Organization.objects.create(name="Quaestio")
        self.user = get_user_model().objects.create_user("soumen@example.test", email="soumen@example.test", password="pw-12345-abc",
                                                         first_name="Soumen", last_name="Das", designation="DBA")
        Membership.objects.create(user=self.user, organization=org, role="editor")
        self.client.force_login(self.user)

    def test_it_shows_who_you_are_and_what_is_set_by_a_super_user(self):
        page = self.client.get(reverse("profile"))
        self.assertContains(page, "Soumen Das")
        for text in ("soumen@example.test", "DBA", "Editor", "Quaestio", "Change password"):
            self.assertContains(page, text)
        self.assertContains(page, '<span class="avatar big" aria-hidden="true">SD</span>')
        self.assertNotContains(page, 'name="designation"')                                  # designation and email are not editable here
        self.assertNotContains(page, 'name="email"')

    def test_changing_the_name_is_saved_audited_and_shown_in_the_header(self):
        response = self.client.post(reverse("profile"), {"name": "  Soumen K. Das  "}, follow=True)
        self.user.refresh_from_db()
        self.assertEqual((self.user.first_name, self.user.last_name, self.user.get_full_name()), ("Soumen K. Das", "", "Soumen K. Das"))
        self.assertContains(response, "Your name was saved.")
        self.assertContains(response, "<strong>Soumen K. Das</strong>")
        self.assertTrue(AuditEntry.objects.filter(action="profile.update", user=self.user, ok=True).exists())
        self.assertEqual((self.user.designation, self.user.email), ("DBA", "soumen@example.test"))      # nothing else changed

    def test_an_empty_name_is_refused_and_nothing_changes(self):
        response = self.client.post(reverse("profile"), {"name": "   "})
        self.assertContains(response, "This field is required")
        self.user.refresh_from_db()
        self.assertEqual(self.user.get_full_name(), "Soumen Das")
        self.assertFalse(AuditEntry.objects.filter(action="profile.update").exists())

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
