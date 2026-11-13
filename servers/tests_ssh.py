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