"""Connecting through an SSH tunnel: a real (in-process) SSH server forwards to the scratch PostgreSQL, and the Add server popup's endpoints."""
import socket
import threading
from unittest import mock

import paramiko
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from access_control.models import Installation, Membership, Organization, OrganizationServer
from audit.models import AuditEntry
from explorer import pg
from explorer import tests as shared
from servers import tunnel
from servers.models import ServerConnection


class FakeSSHServer(paramiko.ServerInterface):
    """Just enough SSH server for the tests: password or public key sign-in, and TCP forwarding (direct-tcpip) to wherever it is asked."""

    def __init__(self, username="tunneller", password="s3cret", authorized_key=None):
        self.username, self.password, self.authorized_key = username, password, authorized_key
        self.host_key = paramiko.ECDSAKey.generate()
        self.destinations = {}
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.listener.close()

    # --- paramiko.ServerInterface
    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == (self.username, self.password) else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        ok = self.authorized_key is not None and username == self.username and key == self.authorized_key
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "direct-tcpip" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        self.destinations[chanid] = destination
        return paramiko.OPEN_SUCCEEDED

    # --- serving
    def _serve(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(client,), daemon=True).start()

    def _session(self, client):
        transport = paramiko.Transport(client)
        transport.add_server_key(self.host_key)
        try:
            transport.start_server(server=self)
            while transport.is_active():
                channel = transport.accept(timeout=1)
                if channel is None:
                    continue
                destination = self.destinations.get(channel.get_id())
                try:
                    onward = socket.create_connection(destination, timeout=5)
                except OSError:
                    channel.close()
                    continue
                threading.Thread(target=tunnel._pump, args=(onward, channel), daemon=True).start()
        except Exception:
            pass
        finally:
            transport.close()


class SSHFixture(shared.PgFixture, TestCase):
    def setUp(self):
        self.fake = FakeSSHServer()
        self.addCleanup(self.fake.close)

    def server(self, **overrides):
        values = dict(name="via ssh", secret="s3cret", ssh_enabled=True, ssh_host="127.0.0.1", ssh_port=self.fake.port, ssh_username="tunneller", ssh_auth="password")
        values.update(overrides)
        name = values.pop("name")
        secret = values.pop("secret", "s3cret")
        server = shared.make_server(**values)
        server.name = name                                    # (make_server always saves a server called "test")
        server.set_ssh_secret(secret)
        server.save()
        return server


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class TunnelTests(SSHFixture):
    def test_a_password_login_reaches_postgresql_through_the_tunnel(self):
        server = self.server()
        self.assertRegex(pg.test_connection(server), r"^\d+")
        server.refresh_from_db()
        self.assertTrue(server.ssh_host_key.startswith("ecdsa-sha2-nistp256 "))             # the SSH server's key was remembered

    def test_a_private_key_login_works_with_and_without_a_passphrase(self):
        import io
        key = paramiko.ECDSAKey.generate()
        self.fake.authorized_key = key
        for passphrase in (None, "open sesame"):
            out = io.StringIO()
            key.write_private_key(out, password=passphrase)
            server = self.server(name=f"key {passphrase}", ssh_auth="key", secret=out.getvalue())
            if passphrase:
                server.set_ssh_passphrase(passphrase)
                server.save()
            with self.subTest(passphrase=passphrase):
                self.assertRegex(pg.test_connection(server), r"^\d+")

    def test_a_key_that_needs_a_passphrase_says_so(self):
        import io
        key = paramiko.ECDSAKey.generate()
        self.fake.authorized_key = key
        out = io.StringIO()
        key.write_private_key(out, password="open sesame")
        with self.assertRaisesMessage(pg.PgError, "protected by a passphrase"):
            pg.test_connection(self.server(ssh_auth="key", secret=out.getvalue()))

    def test_a_wrong_ssh_password_is_reported_plainly(self):
        with self.assertRaisesMessage(pg.PgError, "did not accept the user name 'tunneller' with that password"):
            pg.test_connection(self.server(secret="wrong"))

    def test_a_changed_ssh_server_key_is_refused(self):
        server = self.server()
        server.ssh_host_key = "ecdsa-sha2-nistp256 " + paramiko.ECDSAKey.generate().get_base64()
        server.save()
        with self.assertRaisesMessage(pg.PgError, "identity changed"):
            pg.test_connection(server)

    def test_an_ssh_server_that_is_not_there(self):
        gone = socket.socket()
        gone.bind(("127.0.0.1", 0))
        port = gone.getsockname()[1]
        gone.close()
        with self.assertRaisesMessage(pg.PgError, "Could not reach the SSH server"):
            pg.test_connection(self.server(ssh_port=port))

    def test_postgresql_unreachable_from_the_ssh_server_is_worded_for_the_tunnel(self):
        server = self.server()
        server.port = 1                                                               # nothing listens there, as seen from the SSH server
        server.save()
        with self.assertRaisesMessage(pg.PgError, "worked, but from there PostgreSQL"):
            pg.test_connection(server)

    def test_the_tunnel_is_closed_with_the_connection(self):
        server = self.server()
        opened = []
        real = tunnel.open_tunnel
        with mock.patch("servers.tunnel.open_tunnel", side_effect=lambda s: opened.append(real(s)) or opened[-1]):
            pg.test_connection(server)
        self.assertTrue(opened and opened[0].closed)

    def test_pg_dump_goes_through_the_tunnel_too(self):
        from backups import dump
        server = self.server()
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("backups.dump.subprocess.run", side_effect=fake_run):
            dump._run(["pg_dump", "-h", server.host, "-p", str(server.port), "-U", "u", "-d", "x"], server)
        self.assertEqual(seen["args"][seen["args"].index("-h") + 1], "127.0.0.1")
        self.assertNotEqual(seen["args"][seen["args"].index("-p") + 1], str(server.port))


@override_settings(MODE="shared", SERVER_NETWORKS="", ALLOWED_SERVER_HOSTS=[])
class SharedRuleTests(SSHFixture):
    def test_on_a_shared_installation_the_ssh_server_must_pass_the_address_rule_but_the_database_host_need_not(self):
        with self.assertRaisesMessage(pg.PgError, "private or local network"):
            pg.test_connection(self.server())                                          # the SSH server is on 127.0.0.1: refused
        with override_settings(ALLOWED_SERVER_HOSTS=["127.0.0.1"]):
            self.assertRegex(pg.test_connection(self.server(name="allowed")), r"^\d+")  # the database host (private, via the SSH server) is fine


class PopupEndpointTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)
        self.data = {"name": "Prod", "host": "10.1.2.3", "port": "5432", "username": "pgdas", "password": "pw", "sslmode": "prefer"}

    def test(self, **extra):
        return self.client.post(reverse("server_test_draft"), {**self.data, **extra})

    def create(self, token, **extra):
        return self.client.post(reverse("server_create"), {**self.data, "test_token": token, **extra})

    def test_bad_details_come_back_per_field_and_nothing_is_tested(self):
        with mock.patch("servers.views.pg.test_connection") as tester:
            response = self.test(host="", name="")
        self.assertEqual(response.status_code, 400)
        self.assertIn("host", response.json()["errors"])
        tester.assert_not_called()

    def test_a_failed_test_gives_no_token_and_the_reason(self):
        with mock.patch("servers.views.pg.test_connection", side_effect=pg.PgError("refused for a reason")):
            response = self.test()
        self.assertEqual((response.status_code, response.json()["ok"], response.json()["error"]), (200, False, "refused for a reason"))
        self.assertNotIn("token", response.json())
        self.assertFalse(ServerConnection.objects.exists())

    def test_saving_needs_a_successful_test_of_exactly_these_details(self):
        self.assertEqual(self.create("").status_code, 400)
        self.assertEqual(self.create("forged").json()["ok"], False)
        with mock.patch("servers.views.pg.test_connection", return_value="18.0"):
            token = self.test().json()["token"]
        changed = self.create(token, host="10.9.9.9")
        self.assertEqual(changed.status_code, 400)
        self.assertIn("changed after the test", changed.json()["error"])
        self.assertFalse(ServerConnection.objects.exists())
        saved = self.create(token)
        self.assertEqual((saved.status_code, saved.json()["ok"]), (200, True))
        server = ServerConnection.objects.get(name="Prod")
        self.assertEqual(server.get_password(), "pw")
        self.assertNotIn("pw", server.password_encrypted)
        self.assertTrue(OrganizationServer.objects.filter(server=server, organization=self.org).exists())
        self.assertTrue(AuditEntry.objects.filter(action="server.add").exists())

    def test_an_ssh_server_is_saved_with_its_remembered_key_and_encrypted_secrets(self):
        ssh = {"ssh_enabled": "on", "ssh_host": "bastion.example.com", "ssh_port": "2222", "ssh_username": "jump", "ssh_auth": "password", "ssh_password": "hunter2"}

        def tester(server):
            server.ssh_host_key = "ecdsa-sha2-nistp256 AAAA"                          # what open_tunnel records on first contact
            return "18.0"
        with mock.patch("servers.views.pg.test_connection", side_effect=tester):
            response = self.test(**ssh)
        self.assertTrue(response.json()["ok"], response.content)
        self.assertEqual(self.create(response.json()["token"], **ssh).status_code, 200)
        server = ServerConnection.objects.get(name="Prod")
        self.assertEqual((server.ssh_enabled, server.ssh_host, server.ssh_port, server.ssh_username), (True, "bastion.example.com", 2222, "jump"))
        self.assertEqual(server.get_ssh_secret(), "hunter2")
        self.assertNotIn("hunter2", server.ssh_secret_encrypted)
        self.assertEqual(server.ssh_host_key, "ecdsa-sha2-nistp256 AAAA")
        self.assertIn("via SSH jump@bastion.example.com:2222", AuditEntry.objects.get(action="server.add").detail)

    def test_the_ssh_tab_needs_its_own_details(self):
        response = self.test(ssh_enabled="on")
        self.assertEqual(response.status_code, 400)
        self.assertTrue({"ssh_host", "ssh_username", "ssh_password"} <= set(response.json()["errors"]))
        key_missing = self.test(ssh_enabled="on", ssh_host="b.example.com", ssh_username="u", ssh_auth="key")
        self.assertIn("ssh_key", key_missing.json()["errors"])

    @override_settings(MODE="shared", SERVER_NETWORKS="", ALLOWED_SERVER_HOSTS=[])
    def test_a_private_ssh_host_is_refused_on_a_shared_installation_but_a_private_database_host_behind_it_is_not(self):
        ssh = {"ssh_enabled": "on", "ssh_host": "10.0.0.5", "ssh_username": "u", "ssh_password": "p"}
        self.assertIn("ssh_host", self.test(**ssh).json()["errors"])
        response = self.test(**{**ssh, "ssh_host": "93.184.216.34"})
        self.assertNotIn("host", response.json().get("errors", {}))                    # host 10.1.2.3 is resolved by the SSH server, so it may be private

    def test_only_super_users_can_use_them(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.post(reverse("server_test_draft"), self.data, HTTP_X_REQUESTED_WITH="fetch").status_code, 403)
        self.assertEqual(other.post(reverse("server_create"), self.data, HTTP_X_REQUESTED_WITH="fetch").status_code, 403)

    def test_the_popup_is_on_the_page_for_super_users_only(self):
        page = self.client.get(reverse("server_list"))
        self.assertContains(page, 'id="dlgAddServer"')
        self.assertContains(page, 'name="ssh_enabled"')
        self.assertContains(page, "server_dialog.js")
        self.assertContains(page, "data-server-dialog")

    def test_the_connection_tests_are_rate_limited_for_the_popup_too(self):
        from django.core.cache import cache
        cache.clear()
        with override_settings(SERVER_TESTS_PER_10_MINUTES=1), mock.patch("servers.views.pg.test_connection", return_value="18"):
            self.assertEqual(self.test().status_code, 200)
            self.assertEqual(self.test().status_code, 429)


def key_with_passphrase(passphrase="open sesame"):
    import io
    key = paramiko.ECDSAKey.generate()
    out = io.StringIO()
    key.write_private_key(out, password=passphrase)
    return key, out.getvalue()


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class PromptPassphraseTests(SSHFixture):
    """A key whose passphrase is not stored: a Super User unlocks the server for a while."""

    def setUp(self):
        super().setUp()
        self.key, self.text = key_with_passphrase()
        self.fake.authorized_key = self.key
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)

    def locked_server(self):
        server = self.server(ssh_auth="key", secret=self.text, ssh_prompt_passphrase=True)
        OrganizationServer.objects.get_or_create(server=server, defaults={"organization": self.org})
        return server

    def test_locked_until_a_super_user_unlocks_it_and_locked_again_when_the_time_is_over(self):
        from datetime import timedelta
        from django.utils import timezone
        from servers import unlock
        from servers.models import SSHUnlock
        server = self.locked_server()
        with self.assertRaisesMessage(pg.PgError, "SSH key of this server is locked"):
            pg.test_connection(server)
        unlock.unlock(server, "open sesame", self.admin)
        self.assertRegex(pg.test_connection(server), r"^\d+")
        SSHUnlock.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        with self.assertRaisesMessage(pg.PgError, "locked"):
            pg.test_connection(server)
        self.assertFalse(SSHUnlock.objects.exists())                                 # the expired passphrase is deleted, not just ignored

    def test_the_unlock_endpoint_checks_the_passphrase_and_keeps_it_encrypted(self):
        from servers.models import SSHUnlock
        server = self.locked_server()
        url = reverse("server_unlock", args=[server.pk])
        wrong = self.client.post(url, {"passphrase": "nope"})
        self.assertEqual(wrong.status_code, 400)
        self.assertIn("check the passphrase", wrong.json()["error"])
        self.assertFalse(SSHUnlock.objects.exists())
        self.assertTrue(AuditEntry.objects.filter(action="server.unlock", ok=False).exists())
        right = self.client.post(url, {"passphrase": "open sesame"})
        self.assertEqual((right.status_code, right.json()["ok"]), (200, True))
        row = SSHUnlock.objects.get(server=server)
        self.assertNotIn("open sesame", row.passphrase_encrypted)
        self.assertAlmostEqual((row.expires_at - __import__("django.utils.timezone", fromlist=["now"]).now()).total_seconds(), 8 * 3600, delta=60)
        self.assertTrue(AuditEntry.objects.filter(action="server.unlock", ok=True).exists())
        self.assertIsNotNone(ServerConnection.objects.get(pk=server.pk).ssh_unlocked_until)

    def test_locking_by_hand(self):
        from servers import unlock
        server = self.locked_server()
        unlock.unlock(server, "open sesame", self.admin)
        self.assertEqual(self.client.post(reverse("server_lock", args=[server.pk])).status_code, 200)
        self.assertIsNone(unlock.current(server))

    def test_only_super_users_can_unlock_and_only_servers_that_ask(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        server = self.locked_server()
        other = Client()
        other.force_login(person)
        self.assertEqual(other.post(reverse("server_unlock", args=[server.pk]), {"passphrase": "open sesame"}, HTTP_X_REQUESTED_WITH="fetch").status_code, 403)
        plain = self.server(name="plain")
        OrganizationServer.objects.get_or_create(server=plain, defaults={"organization": self.org})
        self.assertEqual(self.client.post(reverse("server_unlock", args=[plain.pk]), {"passphrase": "x"}).status_code, 400)

    def test_the_buttons_show_the_state_on_the_servers_list(self):
        from servers import unlock
        server = self.locked_server()
        page = self.client.get(reverse("detail") + "?type=servers")
        self.assertContains(page, f'data-server-unlock="{server.pk}"')
        unlock.unlock(server, "open sesame", self.admin)
        page = self.client.get(reverse("detail") + "?type=servers")
        self.assertContains(page, f'data-server-lock="{server.pk}"')
        self.assertNotContains(page, f'data-server-unlock="{server.pk}"')

    def test_the_popup_tests_with_the_typed_passphrase_saves_without_it_and_starts_unlocked(self):
        from servers import unlock
        data = {"name": "Via key", "host": shared.TARGET["HOST"], "port": shared.TARGET["PORT"], "username": shared.TARGET["USER"],
                "password": shared.TARGET["PASSWORD"], "sslmode": "prefer", "ssh_enabled": "on", "ssh_host": "127.0.0.1", "ssh_port": str(self.fake.port),
                "ssh_username": "tunneller", "ssh_auth": "key", "ssh_key": self.text, "ssh_passphrase": "open sesame", "ssh_prompt_passphrase": "on"}
        tested = self.client.post(reverse("server_test_draft"), data)
        self.assertTrue(tested.json()["ok"], tested.content)
        saved = self.client.post(reverse("server_create"), {**data, "test_token": tested.json()["token"]})
        self.assertEqual(saved.status_code, 200, saved.content)
        server = ServerConnection.objects.get(name="Via key")
        self.assertEqual(server.ssh_passphrase_encrypted, "")                         # never stored
        self.assertTrue(server.ssh_prompt_passphrase)
        self.assertIsNotNone(server.ssh_unlocked_until)                               # proven by the test, so it starts unlocked
        self.assertRegex(pg.test_connection(server), r"^\d+")
        unlock.lock(server)
        with self.assertRaisesMessage(pg.PgError, "locked"):
            pg.test_connection(server)

    def test_asking_for_a_passphrase_needs_a_key_and_the_passphrase_once(self):
        base = {"name": "X", "host": "10.1.1.1", "port": "5432", "username": "u", "password": "p", "ssh_enabled": "on", "ssh_host": "93.184.216.34",
                "ssh_username": "u", "ssh_prompt_passphrase": "on"}
        errors = self.client.post(reverse("server_test_draft"), {**base, "ssh_auth": "password", "ssh_password": "x"}).json()["errors"]
        self.assertIn("ssh_prompt_passphrase", errors)
        errors = self.client.post(reverse("server_test_draft"), {**base, "ssh_auth": "key", "ssh_key": self.text}).json()["errors"]
        self.assertIn("ssh_passphrase", errors)

    def test_the_popup_offers_the_key_file_and_the_prompt_option(self):
        page = self.client.get(reverse("server_list"))
        for needle in ("data-key-file", "ssh_prompt_passphrase", "dlgUnlock", "Choose a key file"):
            self.assertContains(page, needle)


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class SavePasswordTests(shared.PgFixture, TestCase):
    """"Save password" off: the PostgreSQL password is not kept; a Super User unlocks the server with it."""

    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)
        self.data = {"name": "No saved pw", "host": shared.TARGET["HOST"], "port": shared.TARGET["PORT"], "username": shared.TARGET["USER"],
                     "password": shared.TARGET["PASSWORD"], "sslmode": "prefer", "save_password": "0"}

    def created(self):
        tested = self.client.post(reverse("server_test_draft"), self.data)
        self.assertTrue(tested.json()["ok"], tested.content)
        saved = self.client.post(reverse("server_create"), {**self.data, "test_token": tested.json()["token"]})
        self.assertEqual(saved.status_code, 200, saved.content)
        server = ServerConnection.objects.get(name="No saved pw")
        OrganizationServer.objects.get_or_create(server=server, defaults={"organization": self.org})
        return server

    def test_the_password_is_tested_but_not_stored_and_the_server_starts_unlocked(self):
        from servers.models import SSHUnlock
        server = self.created()
        self.assertEqual((server.save_password, server.password_encrypted), (False, ""))
        self.assertIsNotNone(server.unlocked_until)
        self.assertRegex(pg.test_connection(server), r"^\d+")
        row = SSHUnlock.objects.get(server=server)
        self.assertNotIn(shared.TARGET["PASSWORD"] or "unlikely", row.password_encrypted)

    def test_locked_servers_say_so_and_unlocking_tests_the_password(self):
        from servers import unlock
        server = self.created()
        unlock.lock(server)
        with self.assertRaisesMessage(pg.PgError, "its password is not saved"):
            pg.test_connection(server)
        url = reverse("server_unlock", args=[server.pk])
        wrong = self.client.post(url, {"password": "definitely-wrong"})
        self.assertEqual(wrong.status_code, 400)
        self.assertNotIn("definitely-wrong", wrong.json()["error"])
        self.assertIsNone(ServerConnection.objects.get(pk=server.pk).unlocked_until)
        self.assertEqual(self.client.post(url, {}).status_code, 400)                       # nothing typed
        right = self.client.post(url, {"password": shared.TARGET["PASSWORD"]})
        self.assertEqual((right.status_code, right.json()["ok"]), (200, True), right.content)
        self.assertRegex(pg.test_connection(ServerConnection.objects.get(pk=server.pk)), r"^\d+")

    def test_pg_dump_is_refused_while_locked_and_gets_the_password_when_unlocked(self):
        from backups import dump
        from servers import unlock
        server = self.created()
        self.assertEqual(dump._environment(server)["PGPASSWORD"], shared.TARGET["PASSWORD"])
        unlock.lock(server)
        with self.assertRaisesMessage(pg.PgError, "its password is not saved"):
            dump._environment(server)

    def test_the_buttons_ask_for_the_password(self):
        from servers import unlock
        server = self.created()
        unlock.lock(server)
        self.assertContains(self.client.get(reverse("detail") + "?type=servers"), 'data-needs-password="1"')

    def test_a_password_is_still_needed_once_for_the_test(self):
        data = {**self.data, "password": ""}
        errors = self.client.post(reverse("server_test_draft"), data).json()["errors"]
        self.assertIn("not saved", errors["password"][0])


@override_settings(SERVER_NETWORKS="any", ALLOWED_SERVER_HOSTS=[])
class KerberosTests(TestCase):
    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)
        self.data = {"name": "Kerb", "host": "db.example.com", "port": "5432", "username": "svc@EXAMPLE.COM", "sslmode": "prefer", "kerberos": "on", "krbsrvname": "pgsvc"}

    def server(self):
        return ServerConnection.objects.create(name="Kerb", host="db.example.com", username="svc@EXAMPLE.COM", kerberos=True, krbsrvname="pgsvc")

    def test_it_is_refused_unless_the_person_who_runs_pgdas_switched_it_on(self):
        with override_settings(KERBEROS=False):
            errors = self.client.post(reverse("server_test_draft"), self.data).json()["errors"]
        self.assertIn("PGDAS_KERBEROS=1", errors["kerberos"][0])
        with override_settings(KERBEROS=False), self.assertRaisesMessage(pg.PgError, "not switched on"):
            pg.test_connection(self.server())

    def test_no_password_is_asked_for_or_sent_and_the_service_name_is_passed(self):
        with override_settings(KERBEROS=True):
            response = self.client.post(reverse("server_test_draft"), self.data)
            self.assertNotIn("password", response.json().get("errors", {}))
            with mock.patch("explorer.pg.psycopg.connect") as connect:
                connect.return_value.execute.return_value.fetchone.return_value = ("18.0",)
                pg.test_connection(self.server())
        kwargs = connect.call_args.kwargs
        self.assertNotIn("password", kwargs)
        self.assertEqual((kwargs["krbsrvname"], kwargs["gssencmode"], kwargs["user"]), ("pgsvc", "prefer", "svc@EXAMPLE.COM"))

    def test_pg_dump_uses_the_machines_ticket_not_a_password(self):
        from backups import dump
        server = self.server()
        with override_settings(KERBEROS=True), mock.patch.dict("os.environ", {"PGPASSWORD": ""}, clear=False):
            env = dump._environment(server)
        self.assertEqual(env["PGKRBSRVNAME"], "pgsvc")
        self.assertFalse(env.get("PGPASSWORD"))
        self.assertFalse(server.password_encrypted)

    def test_saving_keeps_no_password_and_the_default_service_name(self):
        with override_settings(KERBEROS=True), mock.patch("servers.views.pg.test_connection", return_value="18.0"):
            data = {**self.data, "password": "ignored"}
            data.pop("krbsrvname")
            tested = self.client.post(reverse("server_test_draft"), data).json()
            self.assertTrue(tested.get("ok"), tested)
            self.client.post(reverse("server_create"), {**data, "test_token": tested["token"]})
        saved = ServerConnection.objects.get(name="Kerb")
        self.assertEqual((saved.kerberos, saved.password_encrypted, saved.krb_service), (True, "", "postgres"))

    def test_the_check_warns_on_a_shared_installation(self):
        from django.core.checks import run_checks
        with override_settings(MODE="shared", KERBEROS=True):
            self.assertEqual(len([w for w in run_checks() if w.id == "access_control.W006"]), 1)
        with override_settings(MODE="shared", KERBEROS=False):
            self.assertEqual([w for w in run_checks() if w.id == "access_control.W006"], [])


class CommentsAndDatabaseTests(TestCase):
    def test_comments_and_the_first_database_are_saved(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        org = Organization.objects.create(name="Org")
        admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=admin, organization=org, role="super_user")
        client = Client()
        client.force_login(admin)
        data = {"name": "C", "host": "10.1.1.1", "port": "5432", "username": "u", "password": "p", "sslmode": "prefer", "comments": "Reporting replica", "database": "reports"}
        with mock.patch("servers.views.pg.test_connection", return_value="18"):
            token = client.post(reverse("server_test_draft"), data).json()["token"]
            client.post(reverse("server_create"), {**data, "test_token": token})
        saved = ServerConnection.objects.get(name="C")
        self.assertEqual((saved.comments, saved.database, saved.save_password), ("Reporting replica", "reports", True))


class EditPopupTests(TestCase):
    """Edit server in the popup: JSON of the settings (no secrets), a test only when connecting changes, secrets kept when left blank."""

    def setUp(self):
        Installation.objects.update_or_create(pk=1, defaults={"completed": True})
        self.org = Organization.objects.create(name="Org")
        self.admin = get_user_model().objects.create_user("a", email="a@o.test", password="pw-12345-abc", is_staff=True, is_superuser=True)
        Membership.objects.create(user=self.admin, organization=self.org, role="super_user")
        self.client = Client()
        self.client.force_login(self.admin)
        self.server = ServerConnection(name="Prod", host="10.1.2.3", port=5432, username="pgdas", sslmode="prefer", comments="old note",
                                       ssh_enabled=True, ssh_host="93.184.216.34", ssh_port=22, ssh_username="jump", ssh_auth="password",
                                       ssh_host_key="ecdsa-sha2-nistp256 AAAA")
        self.server.set_password("db-secret")
        self.server.set_ssh_secret("ssh-secret")
        self.server.save()
        OrganizationServer.objects.create(server=self.server, organization=self.org)
        self.post = {"name": "Prod", "comments": "old note", "host": "10.1.2.3", "port": "5432", "username": "pgdas", "sslmode": "prefer", "database": "",
                     "ssh_enabled": "on", "ssh_host": "93.184.216.34", "ssh_port": "22", "ssh_username": "jump", "ssh_auth": "password", "save_password": "1"}

    def update(self, token="", **extra):
        return self.client.post(reverse("server_update", args=[self.server.pk]), {**self.post, "test_token": token, **extra})

    def test_the_json_has_every_setting_and_no_secret(self):
        data = self.client.get(reverse("server_json", args=[self.server.pk])).json()
        self.assertEqual(data["values"]["host"], "10.1.2.3")
        self.assertEqual(data["values"]["ssh_username"], "jump")
        self.assertEqual(data["saved"]["password"], True)
        self.assertTrue(data["saved"]["ssh_host_key"].startswith("SHA256:") or data["saved"]["ssh_host_key"] == "")
        text = str(data)
        self.assertNotIn("db-secret", text)
        self.assertNotIn("ssh-secret", text)

    def test_renaming_and_comments_need_no_test(self):
        response = self.update(name="Production", comments="new note")
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True), response.content)
        self.server.refresh_from_db()
        self.assertEqual((self.server.name, self.server.comments), ("Production", "new note"))
        self.assertEqual(self.server.get_password(), "db-secret")                          # nothing typed: kept
        self.assertEqual(self.server.get_ssh_secret(), "ssh-secret")
        self.assertTrue(AuditEntry.objects.filter(action="server.edit").exists())

    def test_changing_how_it_connects_needs_a_test_of_exactly_that(self):
        refused = self.update(host="10.9.9.9")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("press Test connection first", refused.json()["error"])
        with mock.patch("servers.views.pg.test_connection", return_value="18.0") as tester:
            tested = self.client.post(reverse("server_test_draft"), {**self.post, "server_id": self.server.pk, "host": "10.9.9.9"})
        self.assertTrue(tested.json()["ok"], tested.content)
        self.assertEqual(tester.call_args.args[0].get_password(), "db-secret")            # the saved password is used for the test
        wrong = self.update(tested.json()["token"], host="10.8.8.8")
        self.assertEqual(wrong.status_code, 400)
        saved = self.update(tested.json()["token"], host="10.9.9.9")
        self.assertEqual(saved.status_code, 200, saved.content)
        self.server.refresh_from_db()
        self.assertEqual(self.server.host, "10.9.9.9")

    def test_a_typed_password_replaces_the_saved_one_after_a_test(self):
        with mock.patch("servers.views.pg.test_connection", return_value="18.0"):
            tested = self.client.post(reverse("server_test_draft"), {**self.post, "server_id": self.server.pk, "password": "new-secret"})
        self.assertEqual(self.update(tested.json()["token"], password="new-secret").status_code, 200)
        self.server.refresh_from_db()
        self.assertEqual(self.server.get_password(), "new-secret")

    def test_a_test_does_not_change_the_saved_server_or_its_remembered_key(self):
        def tester(server):
            server.ssh_host_key = "ecdsa-sha2-nistp256 BBBB"
            return "18.0"
        with mock.patch("servers.views.pg.test_connection", side_effect=tester):
            self.client.post(reverse("server_test_draft"), {**self.post, "server_id": self.server.pk, "host": "10.9.9.9"})
        self.server.refresh_from_db()
        self.assertEqual((self.server.host, self.server.ssh_host_key), ("10.1.2.3", "ecdsa-sha2-nistp256 AAAA"))

    def test_changing_the_ssh_server_forgets_its_key_and_learns_the_new_one(self):
        def tester(server):
            server.ssh_host_key = "ecdsa-sha2-nistp256 CCCC"
            return "18.0"
        change = {"ssh_host": "93.184.216.35"}
        with mock.patch("servers.views.pg.test_connection", side_effect=tester):
            tested = self.client.post(reverse("server_test_draft"), {**self.post, "server_id": self.server.pk, **change})
        self.assertEqual(self.update(tested.json()["token"], **change).status_code, 200)
        self.server.refresh_from_db()
        self.assertEqual(self.server.ssh_host_key, "ecdsa-sha2-nistp256 CCCC")

    def test_the_name_must_stay_unique_but_may_stay_the_same(self):
        other = ServerConnection(name="Other", host="h", username="u")
        other.set_password("p")
        other.save()
        OrganizationServer.objects.create(server=other, organization=self.org)
        self.assertEqual(self.update(name="Other").status_code, 400)
        self.assertEqual(self.update(name="Prod").status_code, 200)

    def test_only_super_users_and_only_their_own_organizations_servers(self):
        person = get_user_model().objects.create_user("p", email="p@o.test", password="pw-12345-abc")
        Membership.objects.create(user=person, organization=self.org, role="editor")
        other = Client()
        other.force_login(person)
        self.assertEqual(other.get(reverse("server_json", args=[self.server.pk]), HTTP_X_REQUESTED_WITH="fetch").status_code, 403)
        stranger = Organization.objects.create(name="Elsewhere")
        foreign = ServerConnection.objects.create(name="Foreign", host="h", username="u")
        OrganizationServer.objects.create(server=foreign, organization=stranger)
        self.assertEqual(self.client.get(reverse("server_json", args=[foreign.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("server_update", args=[foreign.pk]), self.post).status_code, 404)

    def test_the_popup_has_four_tabs_and_the_edit_buttons(self):
        page = self.client.get(reverse("detail") + "?type=servers")
        self.assertContains(page, f'data-server-edit="{self.server.pk}"')
        shell = self.client.get(reverse("server_list"))
        for needle in ('data-tab="general"', 'data-tab="connection"', 'data-tab="ssh"', 'data-tab="string"', "Save password", "Kerberos authentication",
                       "Database to connect to first", 'name="comments"', "data-current-string"):
            self.assertContains(shell, needle)