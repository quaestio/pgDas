"""Plain-language help when a saved server cannot be reached.

The rule is simple: a remote PostgreSQL server decides who may connect. It must allow the address of the machine PGDAS runs on, otherwise
PostgreSQL (or its firewall) turns PGDAS away, whatever the user name and password. These messages say which of the usual causes it is
and what the person who runs that database has to do, step by step, with the user, database and address filled in.
"""
import re

from django.conf import settings

PG_HBA = re.compile(r'no pg_hba\.conf entry for host "([^"]+)", user "([^"]*)", database "([^"]*)"')


def _pgdas_address(seen=""):
    """The address the database sees PGDAS connecting from: from PostgreSQL's own message when it gave one, else the configured one."""
    return seen or getattr(settings, "PUBLIC_IP", "") or ""


def _where(address):
    return f"this PGDAS server ({address})" if address else "this PGDAS server (ask the person who runs PGDAS for its public IP address)"


def friendly(text, *, host="", port=5432, user="", database=""):
    """A worded explanation for a failed connection, or None when the error is something else."""
    found = PG_HBA.search(text)
    if found:
        address, who, db = found.group(1), found.group(2) or user, found.group(3) or database
        return (f"PostgreSQL at {host or 'the server'} refused this connection because it does not allow {_where(address)} to connect. "
                f"The user name and password were not even checked. What the person who runs that database must do: "
                f"(1) open the file pg_hba.conf on the database machine (Ubuntu: /etc/postgresql/<version>/main/pg_hba.conf) and add the line: "
                f"host  {db or 'all'}  {who or 'all'}  {address}/32  scram-sha-256  "
                f"(2) save it and run: sudo systemctl reload postgresql  "
                f"(3) make sure postgresql.conf has listen_addresses = '*' (or the machine's own address), and the firewall / cloud security group "
                f"allows port {port} from {address}. Then press Test again.")
    low = text.lower()
    if "password authentication failed" in low:
        name = re.search(r'for user "([^"]+)"', text)
        return (f"The server answered, but it did not accept the user name or password{f' (login {name.group(1)!r})' if name else ''}. "
                "Check them in this server's settings (Edit). If they are right, ask the person who runs the database to confirm the login exists "
                "and has a password. (PostgreSQL said: password authentication failed" + (f' for user "{name.group(1)}"' if name else "") + ".)")
    if any(word in low for word in ("timeout expired", "timed out", "connection refused", "no route to host", "network is unreachable")):
        return (f"PGDAS could not reach {host or 'the server'} on port {port}. Usual causes: the address or port is wrong; PostgreSQL is not "
                f"listening for outside connections (postgresql.conf: listen_addresses = '*'); or a firewall / cloud security group does not allow "
                f"{_where(_pgdas_address())} to reach port {port}. The person who runs the database must allow it, then press Test again.")
    if "could not translate host name" in low or "name or service not known" in low or "temporary failure in name resolution" in low:
        return f"The host name {host!r} was not found. Check the spelling of the Host field, or use the server's IP address."
    return None