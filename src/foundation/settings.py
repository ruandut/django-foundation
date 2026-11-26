"""Settings for the foundation apps, as functions a project's settings.py calls.

    import environ
    from foundation import settings as foundation

    env = environ.Env()
    ...
    globals().update(foundation.security(env, secret_key=SECRET_KEY))
    REST_FRAMEWORK = {..., **foundation.rest_framework(env, debug=DEBUG)}
    globals().update(foundation.backups(env, media_root=MEDIA_ROOT, name="my-app"))

Each returns plain settings, so a project can still change any of them afterwards, e.g.
SECURE_CSP["img-src"].append("https://tiles.example.com"). Environment variables they read:

    ALTCHA_HMAC_KEY   signs captcha challenges (openssl rand -hex 32)
    CAPTCHA_ENABLED   "false" skips captcha checks (the widget still runs)
    NUM_PROXIES       proxies in front of Django that append to X-Forwarded-For (default 0)
    HTTPS             "true" once served over https: secure cookies and HSTS
    THROTTLE_ANON, THROTTLE_USER, THROTTLE_AUTH, THROTTLE_CAPTCHA   rates such as "10/min"
    BACKUP_ROOT, PG_DUMP, PG_RESTORE, AWS_S3_BACKUP_BUCKET, AWS_S3_BACKUP_PREFIX,
    AWS_S3_REGION, AWS_S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""

import hashlib
import hmac
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

SECURITY_MIDDLEWARE = [
    # Straight after django.middleware.security.SecurityMiddleware, and above WhiteNoise, so
    # the files it serves get these headers too.
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "foundation.security.middleware.PermissionsPolicyMiddleware",
]

BACKUPS_MIDDLEWARE = [
    # 503 for every request while a database rollback runs. Early, before anything that touches
    # the database: sessions, authentication.
    "foundation.backups.middleware.MaintenanceMiddleware",
]


def security(env, *, secret_key: str | None = None, csp: dict | None = None) -> dict:
    """The cache, captcha and security-header settings.

    `secret_key`: when given and ALTCHA_HMAC_KEY isn't set, the captcha's key is derived from
    it, so an install that predates ALTCHA_HMAC_KEY keeps working. Leave it out to make
    ALTCHA_HMAC_KEY required. `csp`: directives to add to the Content Security Policy's.
    """
    hmac_key = env("ALTCHA_HMAC_KEY", default="")
    if not hmac_key:
        if not secret_key:
            raise ImproperlyConfigured("Set ALTCHA_HMAC_KEY (openssl rand -hex 32).")
        hmac_key = hmac.new(secret_key.encode(), b"foundation.altcha", hashlib.sha256).hexdigest()

    policy = {
        "default-src": [CSP.SELF],
        "script-src": [CSP.SELF],
        # Inline styles stay allowed: React style props, Radix and the captcha widget all set
        # them, and inline CSS can't run code.
        "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
        # Files previewed from blob: URLs; the captcha solves in blob: workers.
        "img-src": [CSP.SELF, "data:", "blob:"],
        "font-src": [CSP.SELF, "data:"],
        "connect-src": [CSP.SELF],
        "worker-src": [CSP.SELF, "blob:"],
        "frame-src": [CSP.SELF, "blob:"],
        "object-src": [CSP.NONE],
        "base-uri": [CSP.SELF],
        "form-action": [CSP.SELF],
        "frame-ancestors": [CSP.NONE],
    }
    for directive, sources in (csp or {}).items():
        policy.setdefault(directive, []).extend(s for s in sources if s not in policy[directive])

    values = {
        # There's no Redis: the cache is a Postgres table (foundation.security's migration),
        # shared by every process. It holds rate-limit counters and used captcha challenges,
        # so it must not cull entries early: a culled challenge could be replayed.
        "CACHES": {
            "default": {
                "BACKEND": "django.core.cache.backends.db.DatabaseCache",
                "LOCATION": "django_cache",
                "OPTIONS": {"MAX_ENTRIES": 50_000},
            }
        },
        # --- Captcha: ALTCHA proof of work (foundation/security/captcha.py) ---
        "ALTCHA_VERIFICATION_ENABLED": env.bool("CAPTCHA_ENABLED", default=True),
        "ALTCHA_HMAC_KEY": hmac_key,
        "ALTCHA_CHALLENGE_EXPIRE": 20 * 60 * 1000,  # ms, altcha's recommended 20 minutes
        "CAPTCHA_CHALLENGE_URL": "/api/auth/captcha/",
        # A challenge can't be redeemed until this long after it was issued, however fast it's
        # solved. The SPA's widget must wait a little longer (its minDuration).
        "CAPTCHA_MIN_SECONDS": 3,
        # The solver tries up to this many SHA-256 hashes. A 2025 laptop does ~500k/s per
        # core, so CAPTCHA_MIN_SECONDS, not this, sets the wait.
        "CAPTCHA_MAX_NUMBER": 500_000,
        # --- Headers. SecurityMiddleware sends nosniff itself. ---
        "SECURE_CSP": policy,
        "SECURE_CROSS_ORIGIN_OPENER_POLICY": "same-origin",
        "SECURE_REFERRER_POLICY": "same-origin",
        "X_FRAME_OPTIONS": "DENY",
        "SESSION_COOKIE_HTTPONLY": True,
    }
    # HTTPS=true once the site is served over https (usually by a proxy in front). Cookies
    # become https-only and browsers are told to stick to https. Redirecting http to https is
    # the proxy's job: healthchecks and the LAN use plain http.
    if env.bool("HTTPS", default=False):
        values.update(
            SESSION_COOKIE_SECURE=True,
            CSRF_COOKIE_SECURE=True,
            SECURE_HSTS_SECONDS=60 * 60 * 24 * 365,
        )
        if env.int("NUM_PROXIES", default=0):
            # Only trust the proxy's word that the request came over https when there is one.
            values["SECURE_PROXY_SSL_HEADER"] = ("HTTP_X_FORWARDED_PROTO", "https")
    return values


def rest_framework(env, *, debug: bool = False, rates: dict | None = None) -> dict:
    """Throttling and rendering keys to merge into REST_FRAMEWORK.

    Every endpoint is limited per account once signed in, and per IP on the public routes
    (DRF checks permissions first, so other anonymous requests are refused before counting).
    Sign-in routes add the stricter "auth" limit (foundation.security.throttling.AuthThrottle).
    `rates`: extra scopes, e.g. {"password": "10/min"}.
    """
    values = {
        "DEFAULT_THROTTLE_CLASSES": [
            "rest_framework.throttling.AnonRateThrottle",
            "rest_framework.throttling.UserRateThrottle",
        ],
        "DEFAULT_THROTTLE_RATES": {
            "anon": env("THROTTLE_ANON", default="120/min"),
            "user": env("THROTTLE_USER", default="1200/min"),
            "auth": env("THROTTLE_AUTH", default="10/min"),
            "captcha": env("THROTTLE_CAPTCHA", default="30/min"),
            **(rates or {}),
        },
        # How many proxies in front of Django append to X-Forwarded-For. 0 trusts only the
        # socket's address, which can't be spoofed. Too low and everyone shares the proxy's
        # limit; too high and clients can pick their own IP. Left unset, DRF trusts it whole.
        "NUM_PROXIES": env.int("NUM_PROXIES", default=0),
    }
    if not debug:
        # No browsable API in production: JSON only.
        values["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]
    return values


def backups(env, *, media_root, name: str) -> dict:
    """Database backup settings (foundation.backups). `name` prefixes dump files and S3 keys;
    MEDIA_ROOT/backups is the default folder, so keep MEDIA_ROOT private (never served)."""
    pg_dump = env("PG_DUMP", default="") or "pg_dump"
    return {
        "BACKUP_ROOT": Path(env("BACKUP_ROOT", default="") or Path(media_root) / "backups"),
        "BACKUP_FILE_PREFIX": name,
        # Amazon S3, when AWS_S3_BACKUP_BUCKET is set. Empty keys use the AWS SDK's own chain
        # (an instance role, ~/.aws), which is how a server should run.
        "BACKUP_S3": {
            "bucket": env("AWS_S3_BACKUP_BUCKET", default=""),
            "prefix": env("AWS_S3_BACKUP_PREFIX", default="") or f"{name}/backups/",
            "region": env("AWS_S3_REGION", default="") or None,
            "endpoint_url": env("AWS_S3_ENDPOINT_URL", default="") or None,
            "access_key_id": env("AWS_ACCESS_KEY_ID", default="") or None,
            "secret_access_key": env("AWS_SECRET_ACCESS_KEY", default="") or None,
        },
        # At least the database server's major version.
        "PG_DUMP": pg_dump,
        # Next to PG_DUMP unless PG_RESTORE says otherwise.
        "PG_RESTORE": env("PG_RESTORE", default="")
        or (str(Path(pg_dump).with_name("pg_restore")) if "/" in pg_dump else "pg_restore"),
        "MAINTENANCE_MESSAGE": (
            "Down for maintenance while the data is restored from a backup. "
            "It will be back shortly."
        ),
    }
