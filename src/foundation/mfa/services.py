"""Two-factor sign-in on allauth.mfa's models and authenticators, without its session-based
flows: what would sit in a session (the app's secret during setup, WebAuthn challenges) is
parked in the cache (Postgres), encrypted, under the person or the second-step token.

The only place that creates, renames or deletes `Authenticator` rows.
"""

import secrets
from dataclasses import dataclass

from allauth.mfa import app_settings as mfa_settings
from allauth.mfa.adapter import get_adapter
from allauth.mfa.models import Authenticator
from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes
from allauth.mfa.totp.internal.auth import TOTP, generate_totp_secret, validate_totp_code
from allauth.mfa.webauthn.internal.auth import (
    WebAuthn,
    build_user_payload,
    get_authenticator_by_credential_id,
    get_credentials,
    get_server,
)
from django.conf import settings
from django.contrib.auth.models import update_last_login
from django.core import signing
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils.module_loading import import_string
from fido2.webauthn import ResidentKeyRequirement, UserVerificationRequirement
from rest_framework_simplejwt.settings import api_settings as jwt_settings

from . import crypto

Type = Authenticator.Type
FACTORS = [Type.TOTP, Type.WEBAUTHN]  # recovery codes only back these up


class Refused(Exception):
    """A 2FA step that can't go ahead; the message is for the person."""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.message = message
        self.code = code


WRONG_CODE = "That code isn't right. Check your app's clock, or use a recovery code."
WRONG_KEY = "That key isn't one of yours, or the request expired. Try again."
LOCKED = "Too many wrong codes. Wait 15 minutes, then sign in again."
EXPIRED = "Signing in took too long. Start again with your password."


# --- reading ------------------------------------------------------------------------------


def authenticators(user):
    return Authenticator.objects.filter(user_id=user.pk)


def is_enabled(user) -> bool:
    """Does signing in need a second factor? Recovery codes alone don't count."""
    return authenticators(user).filter(type__in=FACTORS).exists()


def methods(user) -> list[str]:
    have = set(authenticators(user).values_list("type", flat=True))
    return [t for t in (Type.TOTP, Type.WEBAUTHN, Type.RECOVERY_CODES) if t in have]


def overview(user) -> dict:
    rows = list(authenticators(user).order_by("created_at", "pk"))
    totp = next((a for a in rows if a.type == Type.TOTP), None)
    codes = next((a for a in rows if a.type == Type.RECOVERY_CODES), None)
    return {
        "enabled": any(a.type in FACTORS for a in rows),
        "totp": totp and _describe(totp),
        "keys": [_describe(a) | {"name": a.wrap().name} for a in rows if a.type == Type.WEBAUTHN],
        "recovery_codes": codes
        and {
            "left": len(codes.wrap().get_unused_codes()),
            "total": mfa_settings.RECOVERY_CODE_COUNT,
        },
    }


def _describe(authenticator) -> dict:
    return {
        "id": authenticator.pk,
        "created_at": authenticator.created_at,
        "last_used_at": authenticator.last_used_at,
    }


def key(user, pk) -> Authenticator | None:
    return authenticators(user).filter(type=Type.WEBAUTHN, pk=pk).first()


# --- the authenticator app ------------------------------------------------------------------


def _setup_key(user, what: str) -> str:
    return f"foundation.mfa.setup.{what}:{user.pk}"


def start_totp(user) -> dict:
    if authenticators(user).filter(type=Type.TOTP).exists():
        raise Refused("You already have an authenticator app. Remove it to set up another.")
    secret = generate_totp_secret()
    cache.set(_setup_key(user, "totp"), crypto.encrypt(secret), settings.MFA_SETUP_TIMEOUT)
    adapter = get_adapter()
    url = adapter.build_totp_url(user, secret)
    return {"secret": secret, "url": url, "svg": adapter.build_totp_svg(url)}


def activate_totp(user, code: str) -> list[str] | None:
    """Turns on the app being set up, if `code` is current for it. Returns the recovery codes
    when this is the person's first factor, to be shown once."""
    stored = cache.get(_setup_key(user, "totp"))
    if not stored:
        raise Refused("Start setting up the app first; it may have taken too long.")
    secret = crypto.decrypt(stored)
    if not validate_totp_code(secret, code):
        raise Refused(WRONG_CODE)
    try:
        with transaction.atomic():
            TOTP.activate(user, secret)
            codes = _first_recovery_codes(user)
    except IntegrityError as error:
        raise Refused("You already have an authenticator app.") from error
    cache.delete(_setup_key(user, "totp"))
    return codes


# --- security keys and passkeys ---------------------------------------------------------------


def key_options(user) -> dict:
    """Options for navigator.credentials.create(). Call inside allauth's request context."""
    options, state = get_server().register_begin(
        user=build_user_payload(user),
        credentials=get_credentials(user),
        # Lets phones and laptops make a passkey, while plain security keys still work.
        resident_key_requirement=ResidentKeyRequirement.PREFERRED,
        user_verification=UserVerificationRequirement(mfa_settings.WEBAUTHN_USER_VERIFICATION),
        extensions={"credProps": True},
    )
    cache.set(_setup_key(user, "key"), state, settings.MFA_SETUP_TIMEOUT)
    return dict(options)


def add_key(user, name: str, credential: dict) -> tuple[Authenticator, list[str] | None]:
    """Adds the key that answered the challenge from `key_options`. Call inside allauth's
    request context."""
    state = cache.get(_setup_key(user, "key"))
    if not state:
        raise Refused("Ask for a new challenge and try the key again.")
    try:
        get_server().register_complete(state, credential)
    except Exception as error:  # fido2 raises ValueError, TypeError, KeyError... on bad input
        raise Refused(WRONG_KEY) from error
    cache.delete(_setup_key(user, "key"))
    with transaction.atomic():
        added = WebAuthn.add(user, name, credential).instance
        codes = _first_recovery_codes(user)
    return added, codes


def rename_key(authenticator: Authenticator, name: str) -> None:
    authenticator.data["name"] = name
    authenticator.save(update_fields=["data"])


# --- removing, recovery codes -----------------------------------------------------------------


@transaction.atomic
def remove(authenticator: Authenticator) -> None:
    """Removes a factor; with the last one go the recovery codes, and 2FA is off."""
    user = authenticator.user
    authenticator.delete()
    if not is_enabled(user):
        authenticators(user).filter(type=Type.RECOVERY_CODES).delete()


def _first_recovery_codes(user) -> list[str] | None:
    if authenticators(user).filter(type=Type.RECOVERY_CODES).exists():
        return None
    return RecoveryCodes.activate(user).generate_codes()


@transaction.atomic
def new_recovery_codes(user) -> list[str]:
    if not is_enabled(user):
        raise Refused("Set up an app or a key first.")
    authenticators(user).filter(type=Type.RECOVERY_CODES).delete()
    return RecoveryCodes.activate(user).generate_codes()


# --- the second step of signing in ----------------------------------------------------------

LOGIN_SALT = "foundation.mfa.login"


@dataclass
class Pending:
    """Someone who got their password right and owes a second factor."""

    user: object
    nonce: str


def second_step(user) -> dict:
    """What the login endpoint answers instead of tokens for someone with 2FA."""
    token = signing.dumps({"u": user.pk, "n": secrets.token_urlsafe(16)}, salt=LOGIN_SALT)
    return {"mfa_required": True, "mfa_token": token, "methods": methods(user)}


def pending(token: str) -> Pending:
    from django.contrib.auth import get_user_model

    try:
        data = signing.loads(token or "", salt=LOGIN_SALT, max_age=settings.MFA_LOGIN_TIMEOUT)
    except signing.BadSignature as error:  # also SignatureExpired
        raise Refused(EXPIRED, "expired") from error
    user = get_user_model().objects.filter(pk=data["u"], is_active=True).first()
    if user is None or cache.get(_spent_key(data["n"])):
        raise Refused(EXPIRED, "expired")
    _raise_if_locked(user)
    return Pending(user, data["n"])


def login_key_options(step: Pending) -> dict:
    """Options for navigator.credentials.get(). Call inside allauth's request context."""
    options, state = get_server().authenticate_begin(
        credentials=get_credentials(step.user),
        user_verification=UserVerificationRequirement(mfa_settings.WEBAUTHN_USER_VERIFICATION),
    )
    cache.set(_login_key(step.nonce), state, settings.MFA_LOGIN_TIMEOUT)
    return dict(options)


def finish_with_code(step: Pending, code: str) -> dict:
    authenticator = check_code(step.user, code)
    return _finish(step, authenticator)


def finish_with_key(step: Pending, credential: dict) -> dict:
    """Call inside allauth's request context."""
    state = cache.get(_login_key(step.nonce))
    authenticator = None
    if state:
        try:
            binding = get_server().authenticate_complete(
                state, get_credentials(step.user), credential
            )
            authenticator = get_authenticator_by_credential_id(step.user, binding.credential_id)
        except Exception:  # fido2 raises ValueError, TypeError, KeyError... on bad input
            authenticator = None
    if authenticator is None:
        _failed(step.user)
        raise Refused(WRONG_KEY)
    cache.delete(_login_key(step.nonce))
    authenticator.record_usage()
    return _finish(step, authenticator)


def check_code(user, code: str) -> Authenticator:
    """The authenticator `code` belongs to (an app code, or a recovery code, then spent), or
    Refused. Counts wrong codes towards the lockout."""
    _raise_if_locked(user)
    code = (code or "").strip().replace(" ", "")
    if code:
        for authenticator in authenticators(user).filter(type__in=[Type.TOTP, Type.RECOVERY_CODES]):
            if authenticator.wrap().validate_code(code):
                authenticator.record_usage()
                cache.delete(_failures_key(user))
                return authenticator
    _failed(user)
    raise Refused(WRONG_CODE)


def _finish(step: Pending, authenticator: Authenticator) -> dict:
    # Single use: whoever spends the token first gets the tokens.
    if not cache.add(_spent_key(step.nonce), 1, settings.MFA_LOGIN_TIMEOUT + 60):
        raise Refused(EXPIRED, "expired")
    cache.delete(_failures_key(step.user))
    serializer = import_string(jwt_settings.TOKEN_OBTAIN_SERIALIZER)
    refresh = serializer.get_token(step.user)
    if jwt_settings.UPDATE_LAST_LOGIN:
        update_last_login(None, step.user)
    return {"refresh": str(refresh), "access": str(refresh.access_token)}


def _spent_key(nonce: str) -> str:
    return f"foundation.mfa.login.spent:{nonce}"


def _login_key(nonce: str) -> str:
    return f"foundation.mfa.login.key:{nonce}"


def _failures_key(user) -> str:
    return f"foundation.mfa.failures:{user.pk}"


def _locked_key(user) -> str:
    return f"foundation.mfa.locked:{user.pk}"


def _raise_if_locked(user) -> None:
    if cache.get(_locked_key(user)):
        raise Refused(LOCKED, "locked")


def _failed(user) -> None:
    key = _failures_key(user)
    cache.add(key, 0, settings.MFA_LOCKOUT_SECONDS)
    try:
        failures = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, failures := 1, settings.MFA_LOCKOUT_SECONDS)
    if failures >= settings.MFA_MAX_FAILURES:
        cache.set(_locked_key(user), 1, settings.MFA_LOCKOUT_SECONDS)
        cache.delete(key)
