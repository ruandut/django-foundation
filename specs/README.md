# Specs

Every feature starts as one file here: `NNNN-short-name.md`, copied from
[`_TEMPLATE.md`](_TEMPLATE.md). Ruan writes or approves it, then it gets built.

## Lifecycle

`Draft` → `Approved` → `In progress` → `Done`

A spec is Done when every scenario has a passing test named `test_at_<spec>_<nn>_<what>`, and
`uv run pytest` and `uv run ruff check . && uv run ruff format --check .` pass.

## Writing good scenarios

- From the point of view of the person signing in, the admin, or the project wiring the package
  in: "When I confirm the app with a code from it", not "When I POST".
- One behaviour per scenario, named after the rule it proves.
- Observable outcomes: what the person gets back or sees, not which function ran.
- Say who is acting whenever access matters, and prove the other person can't.
- Invented data only.

## Numbering

0013–0015 were written and built in the project the backups app came from, before it moved
here; their tests (`tests/test_backups.py`, `test_rollback.py`, `test_destinations.py`) came
with it and keep those numbers. New specs continue from 0016.

## Index

| Spec | Title | Status |
|---|---|---|
| 0013 | Database backups: schedules, Hatchet crons, retention, admin | Done (tests only) |
| 0014 | Rolling the database back to a backup, with maintenance mode | Done (tests only) |
| 0015 | Where backups go: a folder or Amazon S3 | Done (tests only) |
| [0016](0016-two-factor-sign-in.md) | Two-factor sign-in: authenticator app, security keys, recovery codes | Done |
