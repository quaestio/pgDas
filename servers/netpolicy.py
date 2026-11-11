"""Which addresses a saved PostgreSQL server may have.

PGDAS connects to the servers people save. On a shared installation with public registration, anybody can become a Super User and
save a "server", so without a rule they could make PGDAS probe the network it runs in: loopback, private ranges, other services,
cloud metadata addresses. With the policy "public" (the default for shared installations) a server must resolve to addresses on the
public internet, unless it is listed in PGDAS_ALLOWED_SERVER_HOSTS. The name is resolved when the server is saved and again at every
connection, and the connection is made to the address that was checked (hostaddr), so changing the DNS answer in between does not help.
"""
import ipaddress
import re
import socket

from django.conf import settings

_HOST = re.compile(r"^[A-Za-z0-9._:\-\[\]%]+$")


class AddressNotAllowed(ValueError):
    pass


def policy():
    chosen = getattr(settings, "SERVER_NETWORKS", "")
    if chosen:
        return chosen
    return "public" if getattr(settings, "MODE", "dedicated") == "shared" else "any"


def _address(text):
    try:
        address = ipaddress.ip_address(str(text).split("%", 1)[0].strip("[]"))
    except ValueError:
        return None
    return address.ipv4_mapped or address if address.version == 6 else address


def _allowed_list():
    names, networks = set(), []
    for entry in getattr(settings, "ALLOWED_SERVER_HOSTS", []):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            names.add(entry.lower())
    return names, networks


def clean_host(host):
    """The host as typed, checked for shape: a name or an address, nothing else (no URL, no spaces, no options)."""
    host = (host or "").strip()
    if not host or len(host) > 253 or not _HOST.match(host) or host.startswith("-"):
        raise AddressNotAllowed("Enter a host name or an IP address, nothing else (no http://, no path, no spaces).")
    return host


def resolve(host):
    """Every address the name resolves to, as ip_address objects. [] when it does not resolve."""
    literal = _address(host)
    if literal is not None:
        return [literal]
    try:
        found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    return list(dict.fromkeys(_address(item[4][0]) for item in found if _address(item[4][0]) is not None))


def check(host):
    """Raise AddressNotAllowed unless the policy lets PGDAS connect to this host. Returns the address to connect to (a string), or ''
    when the policy is "any" and nothing needs pinning."""
    host = clean_host(host)
    if policy() == "any":
        return ""
    names, networks = _allowed_list()
    addresses = resolve(host)
    if not addresses:
        raise AddressNotAllowed("This host name does not resolve to an address.")
    listed = host.lower() in names
    for address in addresses:
        if not (listed or address.is_global or any(address in n for n in networks if n.version == address.version)):
            raise AddressNotAllowed("This server is on a private or local network, which this installation does not allow. "
                                    "Ask the person who runs PGDAS to list it in PGDAS_ALLOWED_SERVER_HOSTS.")
    return str(addresses[0])