# django-foundation

Reusable Django apps for what every internet-facing project needs: protection for its sign-in
routes, security headers, two-factor sign-in, background jobs and database backups. Projects
install it from a release tarball and wire it in through `foundation.settings`.

The [README](README.md) covers installing, wiring and commands; read it first. These are the
rules that aren't obvious from the code.

## Architecture (do not drift without discussing first)

| Concern | Use | Never use instead |
|---|---|---|
| Framework | Django 6.1, DRF, SimpleJWT | other API frameworks, session auth for the API |
| Python tooling | uv (`uv add`, `uv run`), ruff | pip, poetry, black/isort/flake8 |
| Background work, cron | Hatchet (`foundation.jobs`) | Celery, RQ, threads |
| Cache, rate limits, single-use tokens | The Postgres cache table (`foundation.security`) | Redis, local-memory caches |
| Captcha | ALTCHA proof of work | reCAPTCHA, hCaptcha, anything calling a third party |
| Two-factor | `allauth.mfa`'s models and authenticators, behind our own DRF views | django-otp, a TOTP model of our own |
| Database | Postgres | |

## A package, not a project

- Nothing in the package names a project that uses it. Examples say "my-app"; tests build a
  minimal project in `tests/`.
- Settings come from functions in `src/foundation/settings.py` that return plain dicts, so a
  project can change any value afterwards. A new environment variable is read there, listed in
  that module's docstring, and given a safe default or a clear `ImproperlyConfigured`.
- Middleware lists (`SECURITY_MIDDLEWARE`, ...) are exported from `foundation.settings` with a
  comment saying where they go.
- App labels and migrations are forever: installs already have tables named after them. Never
  rename a label (`backups` stays `backups`), and never edit a released migration; add one.
- An app that needs settings registers a system check that names what's missing (see
  `foundation/backups/apps.py`).
- A project that leaves an app out of `INSTALLED_APPS` must not break: other apps check
  `apps.is_installed(...)` before importing it.
- Behaviour a project sees (endpoints, response shapes, settings, env vars) is documented in the
  README in the same change. Breaking one is a new minor version and a line in the README.
- Dependencies are few. A new one needs a reason; optional features go in an extra
  (`[s3]`). When we lean on another library's internal modules (`allauth.mfa.*.internal`), its
  version is pinned to a minor range in `pyproject.toml` and our tests cover what we use.

## Secrets: nothing sensitive in the repo, ever

- No secret, password, token, key or real address is committed, in any file: code, tests,
  docs, examples or commit messages. Test values are invented (`SECRET_KEY = "tests-only"`).
- Settings have no fallback for secrets in production. A missing key fails loudly, except where
  the README documents a derivation (the captcha's key from `SECRET_KEY`).
- Secrets stored in the database (TOTP keys, recovery-code seeds) are encrypted
  (`foundation.mfa.crypto`). Nothing secret is logged or returned twice.
- Before any commit, read the staged diff for secrets.

## Code layout

- One Django app per concern under `src/foundation/`. Inside an app: `models.py`; `querysets.py`
  (a `QuerySet` per model, set with `as_manager()`); `services.py` (writes and workflows, the
  only place that mutates models); `serializers.py`; thin `views.py`; `urls.py`; `tasks.py`
  (`TASKS = [...]`); `admin.py`.
- Apps don't import each other's views. `security` may call `mfa`'s services, guarded by
  `apps.is_installed("foundation.mfa")`.
- Public sign-in routes use `foundation.security`'s throttles (`with_defaults(AuthThrottle)`);
  anything taking a password or a code is throttled per account as well.
- Hatchet tasks take Pydantic inputs carrying ids only, are enqueued in
  `transaction.on_commit`, and sync tasks call `close_old_connections()` around ORM work.
- Tests live in the top-level `tests/` as `test_<area>.py`, against real Postgres. External
  services are faked at their edge (moto for S3, a fake Hatchet client, a software WebAuthn
  authenticator), never by mocking our own code.

## Spec-driven workflow

Specs live in `specs/` (see `specs/README.md`). Ruan writes or approves a spec; then it is built.

A spec is done when, in the same change:
1. Every scenario has a passing test named `test_at_<spec>_<nn>_<what>`.
2. `uv run pytest` and `uv run ruff check . && uv run ruff format --check .` pass.
3. The spec's status is `Done` and its implementation notes are filled in.
4. `specs/README.md` and the README are updated, and this file if a rule changed.

If a spec conflicts with the architecture table or the rules above, stop and raise it first.

## Checking work

- Tests need Postgres at `DATABASE_URL` (default `postgres://postgres:postgres@localhost:5432/foundation`).
- Say plainly what wasn't tested (no real browser for WebAuthn, no real AWS, no real Hatchet
  server), and don't report an inferred cause as fact.

## Git, CI and releases

The flow: branch off `main` → pull request → CI on the PR → merge → CI on `main` → if the
version changed, a release. `main` is always green and always releasable.

- **CI** (`.github/workflows/ci.yml`) runs on every pull request and on every push to `main`,
  as two jobs side by side: `lint` (`ruff check`, `ruff format --check`; installs only the
  `lint` dependency group) and `test` (pytest against a Postgres 18 service, with the Postgres
  18 client for `pg_dump`). A newer push to the same ref cancels the older run.
- **Release** (`.github/workflows/release.yml`) runs when CI finishes on a push to `main`, and
  only if it passed. If `pyproject.toml`'s version has no `v<version>` tag yet, it builds the
  wheel and sdist, tags that commit and publishes a GitHub Release with notes from the merged
  PRs. Otherwise it does nothing, so most merges release nothing. `a`/`b`/`rc`/`.dev` versions
  are published as pre-releases.
- **Rulesets on GitHub** (not in the repo; Settings → Rules): "Protect main" requires a pull
  request, the `lint` and `test` checks, and the branch up to date with `main`; it blocks force
  pushes and deletion, with no bypass, admins included. No approvals are required (Ruan works
  alone and GitHub won't let you approve your own PR). "Protect release tags" stops `v*` tags
  from being moved or deleted; creating them stays open for `release.yml`.
- The job names `lint` and `test` are the required checks. Renaming or adding a required job
  means changing the "Protect main" ruleset in the same change, or every PR waits forever.
- Work on a branch off `main` and land it through a pull request. Pushing to `main` is refused.
  If `main` moves while a PR is open, the PR needs updating ("Update branch") and CI again.
- Commit only when asked. Never push unless asked. Never merge a PR or turn on auto-merge
  unless Ruan asks.
- Releasing is bumping `version` in `pyproject.toml` (and `uv lock`) in a pull request.
  Bump only when Ruan asks, never tag by hand, and never move or delete a published tag:
  projects pin them.
- A new feature, new dependency or anything a project must change is a minor version; a fix
  that asks nothing of projects is a patch. Say what projects must change in the README.
