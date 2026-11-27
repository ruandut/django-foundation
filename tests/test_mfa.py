"""foundation.mfa (spec 0016): two-factor sign-in on allauth.mfa.

App codes are computed from the real secret, as a phone would; security keys are a software
authenticator (tests/webauthn.py) checked by fido2's real server code. Only the captcha is off:
tests/test_security.py covers it.
"""

import time

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import format_hotp_value, hotp_value
from cryptography.fernet import Fernet, InvalidToken
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from foundation import settings as foundation
from foundation.mfa import crypto
from tests.conftest import TEST_PASSWORD, make_user
from tests.webauthn import SoftKey

LOGIN = "/api/auth/token/"
MFA = "/api/auth/mfa/"
SECOND_STEP = "/api/auth/mfa/login/"
Type = Authenticator.Type


@pytest.fixture(autouse=True)
def _setup(settings, monkeypatch):
    settings.ALTCHA_VERIFICATION_ENABLED = False
    # Generous limits, so only the tests about limits meet them.
    _rate(monkeypatch, auth="1000/min", mfa="1000/min")


def _rate(monkeypatch, **overrides):
    # DRF copies the rates onto the class at import, so settings overrides don't reach them.
    monkeypatch.setattr(
        SimpleRateThrottle, "THROTTLE_RATES", {**SimpleRateThrottle.THROTTLE_RATES, **overrides}
    )


@pytest.fixture
def me(db):
    return make_user("me@example.com")


@pytest.fixture
def mine(me):
    client = APIClient()
    client.force_authenticate(me)
    return client


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def code(secret: str, step: int = 0) -> str:
    """The app's code for now (step 0), or the step before or after."""
    return format_hotp_value(hotp_value(secret, int(time.time()) // 30 + step))


def set_up_app(client) -> tuple[str, list[str] | None]:
    secret = client.post(MFA + "totp/setup/").json()["secret"]
    res = client.post(MFA + "totp/", {"code": code(secret), "password": TEST_PASSWORD})
    assert res.status_code == 201, res.content
    return secret, res.json()["recovery_codes"]


def add_key(client, name: str, key: SoftKey | None = None):
    key = key or SoftKey()
    options = client.post(MFA + "keys/options/").json()
    res = client.post(
        MFA + "keys/",
        {"name": name, "credential": key.create(options), "password": TEST_PASSWORD},
        format="json",
    )
    return key, res


def sign_in(username="me", password=TEST_PASSWORD):
    return APIClient().post(LOGIN, {"username": username, "password": password}, format="json")


def mfa_token(username="me") -> str:
    body = sign_in(username).json()
    assert body["mfa_required"] is True
    return body["mfa_token"]


def second_step(token, **body):
    return APIClient().post(SECOND_STEP, {"mfa_token": token, **body}, format="json")


def sign_in_with_key(key: SoftKey, token: str):
    options = APIClient().post(SECOND_STEP + "key-options/", {"mfa_token": token}, format="json")
    assert options.status_code == 200, options.content
    return second_step(token, credential=key.get(options.json()))


def overview(client) -> dict:
    return client.get(MFA).json()


def works_on_the_api(access: str) -> bool:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return client.get("/api/units/").status_code == 200


# --- managing my factors ----------------------------------------------------------------------


def test_at_0016_01_without_2fa_nothing_is_set_up_and_sign_in_is_as_before(mine):
    assert overview(mine) == {"enabled": False, "totp": None, "keys": [], "recovery_codes": None}
    body = sign_in().json()
    assert "mfa_required" not in body
    assert works_on_the_api(body["access"])


def test_at_0016_02_i_set_up_an_authenticator_app_and_get_recovery_codes_once(mine):
    setup = mine.post(MFA + "totp/setup/").json()
    assert setup["url"].startswith("otpauth://totp/")
    assert "issuer=Tests" in setup["url"]
    assert setup["secret"] in setup["url"]
    assert "<svg" in setup["svg"]

    res = mine.post(MFA + "totp/", {"code": code(setup["secret"]), "password": TEST_PASSWORD})
    assert res.status_code == 201
    codes = res.json()["recovery_codes"]
    assert len(codes) == 10 and all(len(c) == 8 and c.isdigit() for c in codes)

    seen = overview(mine)
    assert seen["enabled"] is True
    assert seen["totp"]["id"]
    assert seen["recovery_codes"] == {"left": 10, "total": 10}
    assert not any(c in str(seen) for c in codes)


def test_at_0016_03_a_wrong_code_or_password_doesnt_turn_the_app_on(mine, me):
    res = mine.post(MFA + "totp/", {"code": "123456", "password": TEST_PASSWORD})
    assert res.status_code == 400
    assert "Start setting up" in str(res.json())

    secret = mine.post(MFA + "totp/setup/").json()["secret"]
    wrong = format_hotp_value((int(code(secret)) + 1) % 1_000_000)
    res = mine.post(MFA + "totp/", {"code": wrong, "password": TEST_PASSWORD})
    assert res.status_code == 400
    assert "isn't right" in str(res.json()["code"])

    res = mine.post(MFA + "totp/", {"code": code(secret), "password": "not-mine"})
    assert res.status_code == 400
    assert "password" in res.json()

    assert overview(mine)["enabled"] is False
    assert not Authenticator.objects.filter(user=me).exists()


def test_at_0016_04_i_can_only_have_one_authenticator_app(mine):
    set_up_app(mine)
    res = mine.post(MFA + "totp/setup/")
    assert res.status_code == 400
    assert "Remove it" in res.json()["detail"]


def test_at_0016_05_i_add_security_keys_each_with_a_name(mine):
    _, first = add_key(mine, "Blue YubiKey")
    assert first.status_code == 201, first.content
    assert first.json()["name"] == "Blue YubiKey"
    assert len(first.json()["recovery_codes"]) == 10

    _, second = add_key(mine, "Laptop passkey")
    assert second.status_code == 201
    assert second.json()["recovery_codes"] is None  # made with the first factor only

    seen = overview(mine)
    assert seen["enabled"] is True
    assert [k["name"] for k in seen["keys"]] == ["Blue YubiKey", "Laptop passkey"]


def test_at_0016_06_a_key_that_doesnt_answer_my_challenge_isnt_added(mine, me):
    key = SoftKey()
    # Never asked for a challenge.
    res = mine.post(
        MFA + "keys/",
        {
            "name": "k",
            "credential": key.create({"publicKey": {"challenge": "AAAA"}}),
            "password": TEST_PASSWORD,
        },
        format="json",
    )
    assert res.status_code == 400

    # Answers an older challenge than the one being waited for.
    old = mine.post(MFA + "keys/options/").json()
    mine.post(MFA + "keys/options/")
    res = mine.post(
        MFA + "keys/",
        {"name": "k", "credential": key.create(old), "password": TEST_PASSWORD},
        format="json",
    )
    assert res.status_code == 400
    assert "credential" in res.json()

    # Made for another site.
    options = mine.post(MFA + "keys/options/").json()
    elsewhere = SoftKey(rp_id="evil.example", origin="https://evil.example")
    res = mine.post(
        MFA + "keys/",
        {"name": "k", "credential": elsewhere.create(options), "password": TEST_PASSWORD},
        format="json",
    )
    assert res.status_code == 400

    # Right answer, wrong password.
    options = mine.post(MFA + "keys/options/").json()
    res = mine.post(
        MFA + "keys/",
        {"name": "k", "credential": key.create(options), "password": "not-mine"},
        format="json",
    )
    assert res.status_code == 400
    assert not Authenticator.objects.filter(user=me).exists()


def test_at_0016_07_i_rename_a_key(mine):
    _, res = add_key(mine, "Blue YubiKey")
    pk = res.json()["id"]
    res = mine.patch(f"{MFA}keys/{pk}/", {"name": "Keyring key"}, format="json")
    assert res.status_code == 200
    assert [k["name"] for k in overview(mine)["keys"]] == ["Keyring key"]


def test_at_0016_08_removing_the_last_factor_turns_2fa_off(mine, me):
    set_up_app(mine)
    _, res = add_key(mine, "Blue YubiKey")
    key_url = f"{MFA}keys/{res.json()['id']}/"

    assert mine.delete(key_url, {"password": "not-mine"}, format="json").status_code == 400
    assert len(overview(mine)["keys"]) == 1

    assert mine.delete(key_url, {"password": TEST_PASSWORD}, format="json").status_code == 204
    assert overview(mine)["enabled"] is True  # the app is still there
    res = mine.delete(MFA + "totp/", {"password": TEST_PASSWORD}, format="json")
    assert res.status_code == 204

    assert overview(mine) == {"enabled": False, "totp": None, "keys": [], "recovery_codes": None}
    assert not Authenticator.objects.filter(user=me).exists()
    assert "access" in sign_in().json()


def test_at_0016_09_new_recovery_codes_replace_the_old_ones(mine):
    _, old = set_up_app(mine)
    assert "access" in second_step(mfa_token(), code=old[0]).json()

    res = mine.post(MFA + "recovery-codes/", {"password": TEST_PASSWORD})
    assert res.status_code == 200
    new = res.json()["recovery_codes"]
    assert len(new) == 10 and set(new) != set(old)
    assert overview(mine)["recovery_codes"] == {"left": 10, "total": 10}

    assert second_step(mfa_token(), code=old[1]).status_code == 400
    assert "access" in second_step(mfa_token(), code=new[0]).json()


def test_at_0016_10_i_only_see_and_change_my_own_factors(mine, db):
    them = make_user("them@example.com")
    theirs = client_for(them)
    set_up_app(theirs)
    _, res = add_key(theirs, "Their key")
    url = f"{MFA}keys/{res.json()['id']}/"

    assert mine.patch(url, {"name": "Mine now"}, format="json").status_code == 404
    assert mine.delete(url, {"password": TEST_PASSWORD}, format="json").status_code == 404
    assert mine.delete(MFA + "totp/", {"password": TEST_PASSWORD}, format="json").status_code == 404

    assert [k["name"] for k in overview(theirs)["keys"]] == ["Their key"]
    assert overview(theirs)["totp"] is not None
    assert overview(mine) == {"enabled": False, "totp": None, "keys": [], "recovery_codes": None}


def test_at_0016_11_secrets_are_stored_encrypted_and_need_the_key(mine, me, settings):
    secret, _ = set_up_app(mine)
    totp = Authenticator.objects.get(user=me, type=Type.TOTP)
    codes = Authenticator.objects.get(user=me, type=Type.RECOVERY_CODES)
    seed = crypto.decrypt(codes.data["seed"])
    with connection.cursor() as cursor:
        cursor.execute("SELECT data::text FROM mfa_authenticator WHERE user_id = %s", [me.pk])
        stored = " ".join(row[0] for row in cursor.fetchall())
    assert secret not in stored and seed not in stored
    assert crypto.decrypt(totp.data["secret"]) == secret

    # A project without the key doesn't start; nor with a key that isn't one.
    from tests.test_security import _Env

    with pytest.raises(ImproperlyConfigured):
        foundation.mfa(_Env(), issuer="x")
    with pytest.raises(ImproperlyConfigured):
        foundation.mfa(_Env(MFA_ENCRYPTION_KEY="not-a-key"), issuer="x")

    # Rotating: a new key in front, the old one behind. Existing apps still work.
    settings.MFA_ENCRYPTION_KEYS = [Fernet.generate_key().decode(), *settings.MFA_ENCRYPTION_KEYS]
    assert "access" in second_step(mfa_token(), code=code(secret)).json()
    # ...and new secrets use the new key: the old key alone can't read them.
    mine.post(MFA + "recovery-codes/", {"password": TEST_PASSWORD})
    fresh = Authenticator.objects.get(user=me, type=Type.RECOVERY_CODES).data["seed"]
    settings.MFA_ENCRYPTION_KEYS = settings.MFA_ENCRYPTION_KEYS[1:]
    with pytest.raises(InvalidToken):
        crypto.decrypt(fresh)


def test_at_0016_12_changes_that_take_a_password_are_limited_per_person(mine, monkeypatch):
    set_up_app(mine)
    cache.clear()  # setting up counted too; start the count here
    _rate(monkeypatch, mfa="2/min")
    codes = [
        mine.post(MFA + "recovery-codes/", {"password": "guess"}).status_code for _ in range(3)
    ]
    assert codes == [400, 400, 429]
    # Reading isn't limited by it.
    assert mine.get(MFA).status_code == 200


# --- signing in -------------------------------------------------------------------------------


def test_at_0016_13_with_2fa_my_password_alone_gives_no_tokens(mine):
    set_up_app(mine)
    add_key(mine, "Blue YubiKey")
    res = sign_in()
    assert res.status_code == 200
    body = res.json()
    assert "access" not in body and "refresh" not in body
    assert body["mfa_required"] is True and body["mfa_token"]
    assert body["methods"] == ["totp", "webauthn", "recovery_codes"]
    # A wrong password still gets nothing at all.
    assert sign_in(password="wrong").status_code == 401


def test_at_0016_14_a_code_from_my_app_completes_sign_in(mine):
    secret, _ = set_up_app(mine)
    res = second_step(mfa_token(), code=code(secret, step=1))
    assert res.status_code == 200
    tokens = res.json()
    assert works_on_the_api(tokens["access"])
    refreshed = APIClient().post(
        "/api/auth/token/refresh/", {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == 200
    assert overview(mine)["totp"]["last_used_at"] is not None


def test_at_0016_15_a_recovery_code_completes_sign_in_once(mine):
    _, codes = set_up_app(mine)
    assert works_on_the_api(second_step(mfa_token(), code=codes[3]).json()["access"])
    assert overview(mine)["recovery_codes"] == {"left": 9, "total": 10}
    assert second_step(mfa_token(), code=codes[3]).status_code == 400


def test_at_0016_16_a_security_key_completes_sign_in(mine, db):
    key, _ = add_key(mine, "Blue YubiKey")
    res = sign_in_with_key(key, mfa_token())
    assert res.status_code == 200
    assert works_on_the_api(res.json()["access"])
    assert overview(mine)["keys"][0]["last_used_at"] is not None

    # A key that isn't mine (someone else's, registered to them) gets nothing.
    them = client_for(make_user("them@example.com"))
    their_key, _ = add_key(them, "Their key")
    assert sign_in_with_key(their_key, mfa_token()).status_code == 400

    # Nor does an answer to an earlier challenge.
    token = mfa_token()
    old = APIClient().post(SECOND_STEP + "key-options/", {"mfa_token": token}, format="json")
    APIClient().post(SECOND_STEP + "key-options/", {"mfa_token": token}, format="json")
    assert second_step(token, credential=key.get(old.json())).status_code == 400


def test_at_0016_17_wrong_codes_are_limited_per_person(mine):
    secret, _ = set_up_app(mine)
    token = mfa_token()
    for _ in range(5):
        res = second_step(token, code="000000" if code(secret) != "000000" else "111111")
        assert res.status_code == 400
    res = second_step(token, code=code(secret))
    assert res.status_code == 403
    assert res.json()["code"] == "locked"
    # A new password step doesn't help.
    token = sign_in().json()["mfa_token"]
    assert second_step(token, code=code(secret, step=1)).status_code == 403
    assert "access" not in second_step(token, code=code(secret, step=-1)).json()


def test_at_0016_18_an_app_code_and_a_second_step_token_each_work_once(mine):
    secret, _ = set_up_app(mine)
    used = code(secret, step=-1)
    token = mfa_token()
    assert second_step(token, code=used).status_code == 200
    # The same token again, even with another good code.
    assert second_step(token, code=code(secret, step=1)).status_code == 400
    # The same code on a new token.
    assert second_step(mfa_token(), code=used).status_code == 400


def test_at_0016_19_the_second_step_token_expires_and_cant_be_forged(mine, monkeypatch):
    secret, _ = set_up_app(mine)
    token = mfa_token()

    # Sent as a bearer token, it isn't a JWT.
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert client.get("/api/units/").status_code == 401

    altered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert second_step(altered, code=code(secret)).json()["code"] == "expired"
    forged = signing.dumps({"u": 1, "n": "x"}, salt="something-else")
    assert second_step(forged, code=code(secret)).status_code == 400

    real = time.time
    monkeypatch.setattr(signing.time, "time", lambda: real() + 301)
    res = second_step(token, code=code(secret))
    assert res.status_code == 400 and res.json()["code"] == "expired"


def test_at_0016_20_the_django_admin_login_asks_for_my_code(db, client):
    staff = make_user("boss@example.com", is_staff=True)
    secret, codes = set_up_app(client_for(staff))
    page = client.get("/admin/login/").content.decode()
    assert "Authentication code" in page

    login = {"username": "boss", "password": TEST_PASSWORD}
    res = client.post("/admin/login/", login)
    assert res.status_code == 200 and "_auth_user_id" not in client.session
    assert "That code isn&#x27;t right" in res.content.decode()
    client.post("/admin/login/", {**login, "code": "12345678"})
    assert "_auth_user_id" not in client.session

    assert client.post("/admin/login/", {**login, "code": code(secret)}).status_code == 302
    assert client.session["_auth_user_id"] == str(staff.pk)

    client.logout()
    assert client.post("/admin/login/", {**login, "code": codes[0]}).status_code == 302

    # Staff without 2FA leave it empty.
    client.logout()
    make_user("helper@example.com", is_staff=True)
    res = client.post("/admin/login/", {"username": "helper", "password": TEST_PASSWORD})
    assert res.status_code == 302


def test_at_0016_21_a_superuser_can_turn_off_someones_2fa_in_admin(mine, me, admin_client):
    set_up_app(mine)
    add_key(mine, "Lost key")
    listing = admin_client.get("/admin/mfa/authenticator/").content.decode()
    assert "me" in listing

    ids = list(
        Authenticator.objects.filter(user=me)
        .exclude(type=Type.RECOVERY_CODES)
        .values_list("pk", flat=True)
    )
    res = admin_client.post(
        "/admin/mfa/authenticator/",
        {"action": "delete_selected", "_selected_action": ids, "post": "yes"},
    )
    assert res.status_code == 302
    body = sign_in().json()
    assert "mfa_required" not in body
    assert works_on_the_api(body["access"])
