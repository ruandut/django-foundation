# django-foundation

Reusable Django apps for the parts every internet-facing project needs: protection for its
sign-in routes, security headers, two-factor sign-in, background jobs and database backups. Install it once and
every project built on it shares the same fixes. For Django 6.1, DRF, SimpleJWT, Hatchet and
Postgres, with no Redis.

| App | What it gives you |
|---|---|
| `foundation.security` | An [ALTCHA](https://altcha.org) proof-of-work captcha on sign-in routes and the Django admin's login; rate limits per account and per IP; a Content Security Policy, Permissions-Policy and the other security headers; a Postgres cache table for all of it |
| `foundation.mfa` | Two-factor sign-in on [allauth.mfa](https://docs.allauth.org/en/latest/mfa/): each person adds an authenticator app, security keys or passkeys, and gets recovery codes; signing in (the API and the Django admin) then asks for one. Secrets encrypted at rest |
| `foundation.jobs` | One Hatchet client per process, task discovery (`TASKS = [...]` in each app's `tasks.py`) and `manage.py hatchet_worker` |
| `foundation.backups` | Scheduled `pg_dump` backups run by Hatchet, kept in a folder or Amazon S3 with retention; rolling the database back to one from Django admin, with the app in maintenance mode meanwhile |

## Installing

```bash
uv add "django-foundation @ https://github.com/ruandut/django-foundation/archive/refs/tags/v0.2.0.tar.gz"
```

Releases are listed on the repository's Releases page. A release tarball needs no `git` in the
image that builds your app. For S3 backups, add the
`s3` extra: `django-foundation[s3] @ …`.

## Wiring it in

`settings.py`:

```python
import environ
from foundation import settings as foundation

env = environ.Env()

INSTALLED_APPS = [
    ...,
    "rest_framework",
    "django_altcha",
    "foundation.jobs",
    "foundation.security",
    "foundation.backups",   # optional
    "allauth",              # these four for two-factor sign-in (optional)
    "allauth.account",
    "allauth.mfa",
    "foundation.mfa",
    ...,
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *foundation.SECURITY_MIDDLEWARE,   # above WhiteNoise
    *foundation.BACKUPS_MIDDLEWARE,    # if you use backups
    ...,
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    *foundation.MFA_MIDDLEWARE,        # if you use two-factor sign-in
    ...,
]

REST_FRAMEWORK = {
    ...,
    **foundation.rest_framework(env, debug=DEBUG),
}

# secret_key: derive the captcha's key from it when ALTCHA_HMAC_KEY isn't set. Leave it out to
# make ALTCHA_HMAC_KEY required.
globals().update(foundation.security(env, secret_key=SECRET_KEY))
globals().update(foundation.backups(env, media_root=MEDIA_ROOT, name="my-app"))
globals().update(foundation.mfa(env, issuer="My App"))  # shown in authenticator apps
HATCHET_WORKER_NAME = "my-app-worker"
```

Every helper returns plain settings, so anything can be changed afterwards, e.g.
`SECURE_CSP["img-src"].append("https://tiles.example.com")`. The environment variables they read
are listed at the top of [`foundation/settings.py`](src/foundation/settings.py).

`urls.py`:

```python
from foundation.security.login import protect_admin_login
from foundation.security.views import LoginView

protect_admin_login()  # the captcha on the Django admin's login

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("foundation.security.urls")),  # GET captcha/
    path("api/auth/token/", LoginView.as_view()),  # SimpleJWT behind the captcha (and 2FA)
    path("api/auth/mfa/", include("foundation.mfa.urls")),  # two-factor sign-in
    ...,
]
```

Other views worth protecting take the `CaptchaProtected` mixin
(`foundation.security.views`), or call `foundation.security.captcha.verify(request)` and list
`with_defaults(AuthThrottle)` as their throttles.

### The frontend

The SPA renders the `altcha` widget (npm `altcha` v3) pointed at `/api/auth/captcha/`, with
`minDuration` a little above `CAPTCHA_MIN_SECONDS` (3.5 s for the default 3), and sends its
payload as `altcha` with the form, then fetches a new challenge after each attempt: each one
works once. With backups, a 503 `{"code": "maintenance"}` from the API means a rollback is
running: show a maintenance page, keep the session, and reload once `/api/health/` answers.

### Two-factor sign-in

`MFA_ENCRYPTION_KEY` is required: a Fernet key, made with
`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
To rotate, put a new key first, comma-separated, and keep the old one after it. Security keys
are made for one domain: set `WEBAUTHN_RP_ID` to the site's (e.g. `app.example.com`) when
Django sees another host behind a proxy.

Under `api/auth/mfa/`, for the signed-in person (changes other than renaming take `password`,
and are limited per account at `THROTTLE_MFA`):

| Route | Does |
|---|---|
| `GET ""` | `{enabled, totp, keys: [{id, name, created_at, last_used_at}], recovery_codes: {left, total}}` |
| `POST totp/setup/` | `{secret, url, svg}`: show the QR code (`svg`), or the secret to type in |
| `POST totp/` `{code, password}` | turns the app on; 201 `{recovery_codes}` (a list with the first factor, else null) |
| `DELETE totp/` `{password}` | removes it |
| `POST keys/options/` | `{publicKey}` for `navigator.credentials.create` |
| `POST keys/` `{name, credential, password}` | adds the key; 201 `{id, name, recovery_codes}` |
| `PATCH`/`DELETE keys/<id>/` | `{name}` renames; `{password}` removes |
| `POST recovery-codes/` `{password}` | 10 new codes, replacing the old ones |

Recovery codes are only ever in the response that made them: show them then, once. Removing the
last app or key turns 2FA off and removes the codes.

Signing in: for someone with 2FA, `POST /api/auth/token/` answers
`{"mfa_required": true, "mfa_token": "…", "methods": ["totp", "webauthn", "recovery_codes"]}`
instead of tokens. Ask for a code (app or recovery), or for a key via
`POST api/auth/mfa/login/key-options/ {mfa_token}` and `navigator.credentials.get`; then
`POST api/auth/mfa/login/` with `{mfa_token, code}` or `{mfa_token, credential}` for
`{access, refresh}`. The token lasts 5 minutes and works once; 5 wrong tries lock that
account's second step for 15 minutes (403 `{"code": "locked"}`; `"expired"` means start again
with the password). The Django admin's login gets an "Authentication code" field for app or
recovery codes. A superuser turns off someone's 2FA by deleting their rows under MFA →
Authenticators in admin.

### Backups in production

The image needs `pg_dump` and `pg_restore` at least as new as the Postgres server, and
`BACKUP_ROOT` (default `MEDIA_ROOT/backups`) on a volume. Start the worker once before adding
the first schedule, so Hatchet knows the backup task. Only superusers see backups in admin.

## Developing

Tests run against a real Postgres server, named by `DATABASE_URL` (by default
`postgres://postgres:postgres@localhost:5432/foundation`); pytest creates its own `test_`
database there:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The backup tests that run real `pg_dump`/`pg_restore` skip without them; on a Mac,
`brew install libpq` and run with `PG_DUMP=/opt/homebrew/opt/libpq/bin/pg_dump`.

`foundation.backups` uses the app label `backups`, not `foundation_backups`. Keep it that
way: its tables and migrations are named after it, so changing it would orphan existing
installs' backups.

## Releasing

Changes reach `main` through pull requests, and CI (`.github/workflows/ci.yml`: ruff and
pytest against Postgres 18) runs on each pull request and again on `main` after the merge.

To release, merge a pull request that changes `version` in `pyproject.toml` (and runs
`uv lock`). Once CI passes on `main`, `.github/workflows/release.yml` tags that commit
`vX.Y.Z` and publishes a GitHub Release with the wheel and sdist, its notes made from the
pull requests since the last one. A merge that leaves the version alone releases nothing. A
version such as `0.3.0rc1` is published as a pre-release, for trying a change in one project
first. Projects then move their `uv add` URL to the new tag.

A new feature, a new dependency or anything a project must change is a new minor version
(`0.2.0` → `0.3.0`); a fix that needs nothing of projects is a patch (`0.2.1`).

### 0.2.0

Adds two-factor sign-in (`foundation.mfa`). `django-allauth[mfa]` is now a dependency of every
install; nothing changes for a project that doesn't add the 2FA apps, settings and URLs above.
