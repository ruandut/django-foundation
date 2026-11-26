# django-foundation

The shared plumbing behind Ruan's Django apps (rent-tracker, eden-tracker), so each new app
starts with it and a fix here reaches all of them. For Django 6.1, DRF, SimpleJWT, Hatchet and
Postgres, with no Redis.

| App | What it gives you |
|---|---|
| `foundation.security` | An [ALTCHA](https://altcha.org) proof-of-work captcha on sign-in routes and the Django admin's login; rate limits per account and per IP; a Content Security Policy, Permissions-Policy and the other security headers; a Postgres cache table for all of it |
| `foundation.jobs` | One Hatchet client per process, task discovery (`TASKS = [...]` in each app's `tasks.py`) and `manage.py hatchet_worker` |
| `foundation.backups` | Scheduled `pg_dump` backups run by Hatchet, kept in a folder or Amazon S3 with retention; rolling the database back to one from Django admin, with the app in maintenance mode meanwhile |

## Installing

```bash
uv add "django-foundation @ https://github.com/ruandut/django-foundation/archive/refs/tags/v0.1.0.tar.gz"
```

A release tarball needs no `git` in the image that builds your app. For S3 backups, add the
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
    ...,
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *foundation.SECURITY_MIDDLEWARE,   # above WhiteNoise
    *foundation.BACKUPS_MIDDLEWARE,    # if you use backups
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
    path("api/auth/", include("foundation.security.urls")),     # GET captcha/
    path("api/auth/token/", LoginView.as_view()),               # SimpleJWT behind the captcha
    ...,
]
```

Other views worth protecting take the `CaptchaProtected` mixin
(`foundation.security.views`), or call `foundation.security.captcha.verify(request)` and list
`with_defaults(AuthThrottle)` as their throttles.

### The frontend

The SPA renders the `altcha` widget (npm `altcha` v3) pointed at `/api/auth/captcha/`, with
`minDuration` a little above `CAPTCHA_MIN_SECONDS` (3.5 s for the default 3), and sends its
payload as `altcha` with the form. rent-tracker's `frontend/src/components/altcha.tsx` is a
ready-made React component. With backups, a 503 `{"code": "maintenance"}` from the API means a
rollback is running: eden-tracker shows a maintenance page until `/api/health/` answers.

### Backups in production

The image needs `pg_dump` and `pg_restore` at least as new as the Postgres server, and
`BACKUP_ROOT` (default `MEDIA_ROOT/backups`) on a volume. Start the worker once before adding
the first schedule, so Hatchet knows the backup task. Only superusers see backups in admin.

## Developing

Tests run against a real Postgres (`DATABASE_URL`, by default rent-tracker's dev server on
port 5434; pytest creates its own `test_` database there):

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The backup tests that run real `pg_dump`/`pg_restore` skip without them; on a Mac,
`brew install libpq` and run with `PG_DUMP=/opt/homebrew/opt/libpq/bin/pg_dump`.

`foundation.backups` keeps the app label `backups` and its migration `0001_initial`, so
eden-tracker's existing tables carry over when it switches to this package.
