"""foundation.security: the captcha on sign-in routes, the rate limits and the security headers.

Challenges come from the real endpoint and are solved here as the browser's widget would, so the
HMAC key, salt and replay store are exercised end to end; only the clock is moved forward.
"""

import base64
import json
import time

import altcha
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache, caches
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from foundation import settings as foundation
from foundation.security import captcha

LOGIN = "/api/auth/token/"
CHALLENGE = "/api/auth/captcha/"


@pytest.fixture(autouse=True)
def _captcha_on(settings):
    settings.ALTCHA_VERIFICATION_ENABLED = True
    # Small challenges keep the suite fast; production difficulty is a setting.
    settings.CAPTCHA_MAX_NUMBER = 1000


@pytest.fixture
def later(monkeypatch):
    """Moves the captcha's clock past the minimum solve time."""
    real = time.time
    monkeypatch.setattr(captcha.time, "time", lambda: real() + captcha.settings.CAPTCHA_MIN_SECONDS)


def solve(client=None, *, salt=None) -> str:
    data = (client or APIClient()).get(CHALLENGE).json()
    if salt is not None:
        data["salt"] = salt
    solution = altcha.solve_challenge(
        data["challenge"], data["salt"], data["algorithm"], data["max_number"]
    )
    return base64.b64encode(
        json.dumps(
            {
                "algorithm": data["algorithm"],
                "challenge": data["challenge"],
                "number": solution.number if solution else 0,
                "salt": data["salt"],
                "signature": data["signature"],
            }
        ).encode()
    ).decode()


def login(payload=None, password="pw", client=None):
    body = {"username": "tester", "password": password}
    if payload is not None:
        body["altcha"] = payload
    return (client or APIClient()).post(LOGIN, body, format="json")


# --- challenge -----------------------------------------------------------------------------


def test_challenge_is_public_uncached_and_carries_its_issue_time(db):
    res = APIClient().get(CHALLENGE)
    assert res.status_code == 200
    data = res.json()
    assert {"algorithm", "challenge", "salt", "signature", "max_number"} <= data.keys()
    assert res["Cache-Control"] == "no-store"
    assert abs(captcha.issued_at(solve()) - time.time()) < 5


def test_challenge_ignores_a_stale_token(db):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
    assert client.get(CHALLENGE).status_code == 200


def test_challenge_only_answers_get(db):
    assert APIClient().post(CHALLENGE).status_code == 405


# --- login ---------------------------------------------------------------------------------


def test_login_with_a_solved_captcha(user, later):
    res = login(solve())
    assert res.status_code == 200, res.json()
    assert {"access", "refresh"} <= res.json().keys()


def test_login_needs_the_captcha(user, later):
    res = login()
    assert res.status_code == 400
    assert res.json() == {"altcha": ["Complete the captcha first."]}


def test_captcha_is_checked_before_the_password(user, later):
    # A wrong password with no captcha says nothing about the password.
    assert login(password="wrong").json() == {"altcha": ["Complete the captcha first."]}
    assert login(solve(), password="wrong").status_code == 401


def test_a_solved_captcha_works_once(user, later):
    payload = solve()
    assert login(payload, password="wrong").status_code == 401
    res = login(payload)
    assert res.status_code == 400
    assert res.json() == {"altcha": ["Challenge has already been used."]}


def test_solving_faster_than_the_minimum_is_rejected(user):
    payload = solve()
    res = login(payload)
    assert res.status_code == 400
    assert res.json() == {"altcha": [captcha.TOO_FAST]}
    # And that payload is spent, so waiting doesn't revive it.
    real = time.time
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(captcha.time, "time", lambda: real() + 60)
        assert login(payload).json() == {"altcha": ["Challenge has already been used."]}


def test_the_issue_time_cannot_be_backdated(user, later):
    data = APIClient().get(CHALLENGE).json()
    backdated = data["salt"].replace("issued=", "issued=1")
    res = login(solve(salt=backdated))
    assert res.status_code == 400
    assert res.json() == {"altcha": ["Invalid CAPTCHA token."]}


def test_a_challenge_signed_with_another_key_is_rejected(user, later, settings):
    settings.ALTCHA_HMAC_KEY = "someone-elses-key"
    payload = solve()
    settings.ALTCHA_HMAC_KEY = "the-real-key"
    assert login(payload).json() == {"altcha": ["Invalid CAPTCHA token."]}


def test_a_challenge_without_an_issue_time_is_rejected(user, later):
    # Validly signed by django-altcha's own helper, but not issued by our endpoint.
    from django_altcha import get_altcha_challenge

    c = get_altcha_challenge(max_number=1000)
    n = altcha.solve_challenge(c).number
    payload = altcha.Payload(c.algorithm, c.challenge, n, c.salt, c.signature).to_base64()
    assert login(payload).json() == {"altcha": ["Invalid CAPTCHA token."]}


@pytest.mark.parametrize("junk", ["", "bm90LWpzb24=", "!!!", "eyJ9"])
def test_junk_payloads_are_rejected(user, later, junk):
    assert login(junk).status_code == 400


def test_captcha_enabled_false_skips_it(user, settings):
    settings.ALTCHA_VERIFICATION_ENABLED = False
    assert login().status_code == 200


# --- admin login ---------------------------------------------------------------------------


@pytest.fixture
def staff(db):
    return get_user_model().objects.create_superuser("boss", password="pw")


def test_admin_login_page_shows_the_widget(db, client):
    html = client.get("/admin/login/").content.decode()
    assert "<altcha-widget" in html
    assert 'challengeurl="/api/auth/captcha/"' in html
    assert "/static/altcha/altcha.min.js" in html


def test_admin_login_needs_the_captcha(staff, client, later):
    res = client.post("/admin/login/", {"username": "boss", "password": "pw"})
    assert res.status_code == 200
    assert "Complete the captcha first." in res.content.decode()
    assert "_auth_user_id" not in client.session

    res = client.post("/admin/login/", {"username": "boss", "password": "pw", "altcha": solve()})
    assert res.status_code == 302
    assert client.session["_auth_user_id"] == str(staff.pk)


# --- rate limits ---------------------------------------------------------------------------


def test_the_cache_is_shared_through_postgres(db):
    # Rate limits and used challenges must hold across gunicorn workers.
    assert type(caches["default"]).__name__ == "DatabaseCache"
    cache.set("probe", 1)
    assert cache.get("probe") == 1


def test_login_is_limited_per_ip(user, settings, monkeypatch):
    settings.ALTCHA_VERIFICATION_ENABLED = False
    _rate(monkeypatch, auth="3/min")
    codes = [login(password="wrong").status_code for _ in range(4)]
    assert codes == [401, 401, 401, 429]
    # The limit is per IP: a new client from the same address is still blocked...
    assert login().status_code == 429
    # ...and X-Forwarded-For isn't trusted unless NUM_PROXIES says so.
    spoofed = APIClient(HTTP_X_FORWARDED_FOR="203.0.113.9")
    assert login(client=spoofed).status_code == 429


def test_signed_in_requests_are_limited_per_account(api, monkeypatch):
    _rate(monkeypatch, user="2/min")
    assert [api.get("/api/units/").status_code for _ in range(3)] == [200, 200, 429]


def test_anonymous_requests_are_limited_per_ip(db, monkeypatch):
    _rate(monkeypatch, anon="2/min")
    # Public routes only: DRF checks permissions first, so a protected one 401s before this.
    refresh = {"refresh": "not-a-token"}
    codes = [
        APIClient().post("/api/auth/token/refresh/", refresh, format="json").status_code
        for _ in range(3)
    ]
    assert codes == [401, 401, 429]
    # The healthcheck is never limited, or Docker would restart a healthy app.
    assert all(APIClient().get("/api/health/").status_code == 200 for _ in range(5))


def _rate(monkeypatch, **overrides):
    # DRF copies the rates onto the class at import, so settings overrides don't reach them.
    monkeypatch.setattr(
        SimpleRateThrottle, "THROTTLE_RATES", {**SimpleRateThrottle.THROTTLE_RATES, **overrides}
    )


# --- security headers ----------------------------------------------------------------------


def _assert_hardened(res):
    csp = res["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "worker-src 'self' blob:" in csp
    assert res["X-Frame-Options"] == "DENY"
    assert res["X-Content-Type-Options"] == "nosniff"
    assert res["Referrer-Policy"] == "same-origin"
    assert res["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "camera=()" in res["Permissions-Policy"]


def test_api_responses_carry_the_security_headers(db, client):
    _assert_hardened(client.get("/api/health/"))


def test_the_admin_carries_the_security_headers(db, client):
    _assert_hardened(client.get("/admin/login/"))


def test_https_mode_hardens_cookies_and_sends_hsts(db, client, settings):
    settings.SECURE_HSTS_SECONDS = 31536000
    res = client.get("/api/health/", secure=True)
    assert res["Strict-Transport-Security"].startswith("max-age=31536000")


# --- views and settings helpers ------------------------------------------------------------


def test_the_admin_widget_follows_the_settings(db, client, settings):
    settings.CAPTCHA_CHALLENGE_URL = "/elsewhere/captcha/"
    settings.CAPTCHA_MIN_SECONDS = 5
    html = client.get("/admin/login/").content.decode()
    assert 'challengeurl="/elsewhere/captcha/"' in html
    assert 'delay="5500"' in html


def test_captcha_protected_views_only_check_posts(user, later):
    # GET on the token view is refused for its method, not for a missing captcha.
    assert APIClient().get(LOGIN).status_code == 405


class _Env:
    """environ.Env stand-in with fixed values."""

    def __init__(self, **values):
        self.values = values

    def __call__(self, name, default=None):
        return self.values.get(name, default)

    def bool(self, name, default=False):
        value = self.values.get(name)
        return default if value is None else value in ("true", "1", True)

    def int(self, name, default=0):
        return int(self.values.get(name, default))


def test_the_captcha_key_comes_from_the_environment_or_the_secret_key():
    assert foundation.security(_Env(ALTCHA_HMAC_KEY="k"))["ALTCHA_HMAC_KEY"] == "k"
    derived = foundation.security(_Env(), secret_key="s")["ALTCHA_HMAC_KEY"]
    assert len(derived) == 64 and derived != "s"
    # Without either it fails rather than run with a guessable key.
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured):
        foundation.security(_Env())


def test_the_csp_can_be_extended():
    csp = foundation.security(
        _Env(ALTCHA_HMAC_KEY="k"), csp={"img-src": ["https://tiles.example.com", "data:"]}
    )["SECURE_CSP"]
    assert csp["img-src"].count("data:") == 1
    assert "https://tiles.example.com" in csp["img-src"]


def test_https_trusts_the_proxy_header_only_behind_a_proxy():
    direct = foundation.security(_Env(ALTCHA_HMAC_KEY="k", HTTPS="true"))
    assert direct["SECURE_HSTS_SECONDS"] and direct["SESSION_COOKIE_SECURE"]
    assert "SECURE_PROXY_SSL_HEADER" not in direct
    proxied = foundation.security(_Env(ALTCHA_HMAC_KEY="k", HTTPS="true", NUM_PROXIES="1"))
    assert proxied["SECURE_PROXY_SSL_HEADER"] == ("HTTP_X_FORWARDED_PROTO", "https")
    assert "SECURE_HSTS_SECONDS" not in foundation.security(_Env(ALTCHA_HMAC_KEY="k"))


def test_the_browsable_api_is_only_on_in_debug():
    assert "DEFAULT_RENDERER_CLASSES" not in foundation.rest_framework(_Env(), debug=True)
    off = foundation.rest_framework(_Env(), debug=False)
    assert off["DEFAULT_RENDERER_CLASSES"] == ["rest_framework.renderers.JSONRenderer"]
    rates = foundation.rest_framework(_Env(), rates={"password": "10/min"})
    assert rates["DEFAULT_THROTTLE_RATES"]["password"] == "10/min"


def test_task_discovery_only_looks_in_the_chosen_apps(settings):
    from foundation.jobs.registry import discover_tasks

    settings.HATCHET_TASK_APPS = ("nothing.",)
    assert discover_tasks() == []
