"""Client addresses and the allowed-address list of an organization."""
import ipaddress
import re

from django.conf import settings

_SPLIT = re.compile(r"[\s,;]+")


def parse_networks(text):
    """(networks, bad entries) from a list written one per line (commas and spaces also separate; # starts a comment)."""
    networks, bad = [], []
    for line in (text or "").splitlines():
        for entry in _SPLIT.split(line.split("#", 1)[0].strip()):
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                bad.append(entry)
    return networks, bad


def normalise(text):
    """The list, one entry per line, each as written by the network (10.0.0.5 stays 10.0.0.5, 10.0.0.9/24 becomes 10.0.0.0/24)."""
    networks, _ = parse_networks(text)
    return "\n".join(str(n.network_address) if n.num_addresses == 1 else str(n) for n in networks)


def _address(value):
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    return address.ipv4_mapped or address if address.version == 6 else address


def _in(address, networks):
    return any(address.version == n.version and address in n for n in networks)


def client_ip(request):
    """The address the request really came from. X-Forwarded-For is believed only when the connection comes from a proxy listed in
    PGDAS_TRUSTED_PROXIES, and then the first address from the right that is not itself a trusted proxy is used."""
    remote = _address(request.META.get("REMOTE_ADDR", ""))
    trusted, _ = parse_networks("\n".join(getattr(settings, "TRUSTED_PROXIES", [])))
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if remote is None or not trusted or not _in(remote, trusted) or not forwarded:
        return remote
    for part in reversed([p for p in forwarded.split(",") if p.strip()]):
        address = _address(part)
        if address is None:
            return None                                  # a malformed header is not trusted at all
        if not _in(address, trusted):
            return address
    return remote


def allowed(text, address):
    """True when the list is empty, or the address is in it."""
    networks, _ = parse_networks(text)
    if not networks:
        return True
    address = _address(address) if address is not None else None
    return address is not None and _in(address, networks)