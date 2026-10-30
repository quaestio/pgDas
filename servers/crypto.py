"""Encryption of saved server passwords. Passwords are never stored or logged in plain text."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = settings.ENCRYPTION_KEY
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    return Fernet(key)


def encrypt(plain):
    return _fernet().encrypt((plain or "").encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt((token or "").encode()).decode()
    except InvalidToken:
        # Wrong or rotated key: report it instead of leaking a traceback with the token.
        raise ValueError("The saved password cannot be decrypted. Re-enter it on the server's edit page.")
