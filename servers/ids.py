"""Server numbers in links and requests, as encrypted tokens: ?server=Zk3x… instead of ?server=2.

The number is enciphered with a small keyed Feistel network (four rounds, HMAC-SHA256 as the round function, a key derived from the
installation's secret key) and sealed with a short HMAC tag, then written in URL-safe base64. The same server always gets the same token (links
and bookmarks keep working), a token cannot be altered or made up without the key, and the number cannot be read from it.

This hides the number; it is not an access check. Every request is still checked against what the signed-in person may see, exactly as before.

Plain numbers are still understood when they come in (old bookmarks, scripts), unless PGDAS_STRICT_SERVER_IDS=1, which refuses them. The number 0
stays "0": the pages use it as the placeholder in an address they fill in later.
"""
import base64
import hashlib
import hmac

from django.conf import settings

_ROUNDS = 4
_TAG = 6


def _key():
    secret = (getattr(settings, "ENCRYPTION_KEY", "") or settings.SECRET_KEY).encode()
    return hashlib.sha256(b"pgdas-server-id|" + secret).digest()


def _round(key, index, half):
    return int.from_bytes(hmac.new(key, bytes([index]) + half.to_bytes(4, "big"), hashlib.sha256).digest()[:4], "big")


def encode(number):
    """The token for a server number."""
    number = int(number)
    if number <= 0:
        return str(number) if number == 0 else ""
    if number >= 1 << 64:
        return ""
    key = _key()
    left, right = number >> 32, number & 0xFFFFFFFF
    for index in range(_ROUNDS):
        left, right = right, left ^ _round(key, index, right)
    block = ((left << 32) | right).to_bytes(8, "big")
    tag = hmac.new(key, b"tag" + block, hashlib.sha256).digest()[:_TAG]
    return base64.urlsafe_b64encode(block + tag).decode().rstrip("=")


def decode(value):
    """The server number a token stands for, or None when it is not one of ours (or not a number at all)."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if text.isdigit():                                   # a plain number: an old link
        if text == "0" or not getattr(settings, "PGDAS_STRICT_SERVER_IDS", False):
            return int(text)
        return None
    if len(text) != 19 or not all(c.isalnum() or c in "-_" for c in text):
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        return None
    if len(raw) != 8 + _TAG:
        return None
    key, block, tag = _key(), raw[:8], raw[8:]
    if not hmac.compare_digest(hmac.new(key, b"tag" + block, hashlib.sha256).digest()[:_TAG], tag):
        return None
    left, right = int.from_bytes(block[:4], "big"), int.from_bytes(block[4:], "big")
    for index in reversed(range(_ROUNDS)):
        left, right = right ^ _round(key, index, left), left
    return (left << 32) | right


def to_digits(value):
    """The plain number as text (what the views already work with), or the text itself when it is not a token, so a bad value stays a bad value."""
    found = decode(value)
    text = str(value if value is not None else "")
    if found is not None:
        return str(found)
    return "not-a-server" if text.isdigit() else text           # a plain number refused (PGDAS_STRICT_SERVER_IDS) must not pass as a number


def to_token(value):
    """The token for a number given as a number or as text; a token (or anything else) is returned unchanged."""
    text = str(value if value is not None else "")
    return encode(text) if text.isdigit() and text != "0" else text


class ServerIdConverter:
    """URL converter `sid`: /c/<sid:pk>/ matches a token (or an old plain number) and gives the view the number; reverse() writes the token."""
    regex = r"[A-Za-z0-9_-]+"

    def to_python(self, value):
        found = decode(value)
        if found is None:
            raise ValueError(value)
        return found

    def to_url(self, value):
        if isinstance(value, str) and not value.isdigit():       # already a token (a page that holds the token, not the number)
            if decode(value) is None:
                raise ValueError(value)
            return value
        return encode(value)
