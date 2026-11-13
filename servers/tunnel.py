"""Reaching a PostgreSQL server through an SSH server (a "bastion").

PGDAS signs in to the SSH server, then asks it to open a connection to the PostgreSQL host and port (as the SSH server sees them), and
connects PostgreSQL's client to a local port that leads there. Nothing is installed on either server.

* The SSH server's key is remembered the first time and must be the same afterwards (otherwise somebody could stand in for it).
* On a shared installation the SSH server itself must pass the same address rule as any saved server (see netpolicy); the PostgreSQL host
  is resolved by the SSH server, so it may be a private address there: that is what a bastion is for.
* The password or private key is kept encrypted and is only used here.
"""
import base64
import hashlib
import io
import select
import socket
import threading

import paramiko

from . import netpolicy


class SSHError(Exception):
    """A problem with the SSH connection, worded for the person who saved the server. Never contains a password or key."""


def key_text(key):
    return f"{key.get_name()} {key.get_base64()}"


def fingerprint(text):
    """The usual SHA256 fingerprint of a stored "type base64" key, for showing to people."""
    try:
        raw = base64.b64decode(text.split()[1])
    except Exception:
        return ""
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def _load_private_key(text, passphrase):
    classes = [getattr(paramiko, name) for name in ("Ed25519Key", "ECDSAKey", "RSAKey", "DSSKey") if hasattr(paramiko, name)]
    problem = None
    for cls in classes:
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise SSHError("This private key is protected by a passphrase. Enter it in the Passphrase field.")
        except paramiko.SSHException as error:
            problem = error
        except Exception as error:
            problem = error
    raise SSHError("The private key could not be read. Paste the whole private key text, including the BEGIN and END lines"
                   + (" (and check the passphrase)." if passphrase else "."))


class Tunnel:
    def __init__(self, transport, listener, thread):
        self.transport, self.listener, self.thread = transport, listener, thread
        self.local_port = listener.getsockname()[1]
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        for closer in (self.listener.close, self.transport.close):
            try:
                closer()
            except Exception:
                pass


def _pump(client, channel):
    try:
        while True:
            readable, _, _ = select.select([client, channel], [], [], 1.0)
            if client in readable:
                data = client.recv(32768)
                if not data:
                    break
                channel.sendall(data)
            if channel in readable:
                data = channel.recv(32768)
                if not data:
                    break
                client.sendall(data)
    except Exception:
        pass
    finally:
        for end in (client, channel):
            try:
                end.close()
            except Exception:
                pass


def _accept_loop(transport, listener, destination):
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return                                          # the listener was closed: the tunnel is finished
        try:
            channel = transport.open_channel("direct-tcpip", destination, client.getsockname())
        except Exception:
            client.close()
            continue
        threading.Thread(target=_pump, args=(client, channel), daemon=True).start()


def open_tunnel(server):
    """Sign in to the SSH server and return a Tunnel to server.host:server.port. May record the SSH server's key on the server object
    (and in the database when the server is saved). Raises SSHError."""
    try:
        pinned = netpolicy.check(server.ssh_host)
    except netpolicy.AddressNotAllowed as error:
        raise SSHError(str(error))
    secret = server.get_ssh_secret()
    if not server.ssh_host or not server.ssh_username or not secret:
        raise SSHError("The SSH tunnel needs the SSH host, user name and a password or private key.")
    try:
        sock = socket.create_connection((pinned or server.ssh_host, int(server.ssh_port or 22)), timeout=10)
    except OSError as error:
        raise SSHError(f"Could not reach the SSH server {server.ssh_host}:{server.ssh_port} ({error.__class__.__name__}). Check the address and port, "
                       "and that its firewall allows this PGDAS server.")
    transport = paramiko.Transport(sock)
    try:
        try:
            transport.start_client(timeout=15)
        except (paramiko.SSHException, EOFError, OSError) as error:
            raise SSHError(f"{server.ssh_host}:{server.ssh_port} did not answer as an SSH server ({error.__class__.__name__}).")
        seen = key_text(transport.get_remote_server_key())
        stored = (server.ssh_host_key or "").strip()
        if stored and stored != seen:
            raise SSHError(f"The SSH server's identity changed (it now presents {fingerprint(seen)}, not {fingerprint(stored)}). If you expected that, "
                           "edit the server and clear the saved SSH host key; otherwise somebody may be standing in for that server.")
        if not stored:
            server.ssh_host_key = seen
            if server.pk:
                type(server).objects.filter(pk=server.pk).update(ssh_host_key=seen)
        try:
            if server.ssh_auth == "key":
                transport.auth_publickey(server.ssh_username, _load_private_key(secret, server.get_ssh_passphrase()))
            else:
                transport.auth_password(server.ssh_username, secret)
        except paramiko.AuthenticationException:
            raise SSHError(f"The SSH server did not accept the user name {server.ssh_username!r} with that {'private key' if server.ssh_auth == 'key' else 'password'}.")
        except paramiko.SSHException as error:
            raise SSHError(f"SSH sign-in failed ({error.__class__.__name__}).")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        thread = threading.Thread(target=_accept_loop, args=(transport, listener, (server.host, int(server.port))), daemon=True)
        tunnel = Tunnel(transport, listener, thread)
        thread.start()
        return tunnel
    except BaseException:
        transport.close()
        raise