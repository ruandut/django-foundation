"""Encryption for the 2FA secrets allauth.mfa keeps in the database (TOTP keys, recovery-code
seeds), and for setup state parked in the cache. Fernet, with settings.MFA_ENCRYPTION_KEYS: the
first key encrypts, any of them decrypts, so keys can be rotated."""

from functools import cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings


@cache
def _fernet(keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(key) for key in keys])


def _current() -> MultiFernet:
    return _fernet(tuple(settings.MFA_ENCRYPTION_KEYS))


def encrypt(text: str) -> str:
    return _current().encrypt(text.encode()).decode()


def decrypt(token: str) -> str:
    return _current().decrypt(token.encode()).decode()
