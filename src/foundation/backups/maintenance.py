"""Maintenance mode: while a rollback runs, the app is down for everyone.

The flag is a Postgres advisory lock, held by the worker on a connection of its own for as long
as the rollback runs. It lives outside every table, so restoring the database can't touch it,
and it goes by itself if the worker dies: Postgres releases a session's locks when it ends. Every
process sees it, whichever machine it runs on.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from django.db import connection

# (classid, objid) of the advisory lock. Arbitrary, fixed; never change it, or a rollback
# running across an upgrade would go unseen. Spells "eden" / "roll", where it began.
LOCK = (0x6564656E, 0x726F6C6C)

IN_PROGRESS = "maintenance"
RETRY_AFTER_SECONDS = 30


def in_progress() -> bool:
    """Is a rollback holding the maintenance lock? Reads pg_locks, which takes no table locks,
    so it answers at once even mid-restore."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory' AND granted AND objsubid = 2
                  AND classid = %s AND objid = %s
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
            )
            """,
            LOCK,
        )
        return cursor.fetchone()[0]


def _connect() -> psycopg.Connection:
    """A connection of its own to the app's database, outside Django's pool."""
    params = connection.get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    return psycopg.connect(**params, autocommit=True)


class AlreadyInProgress(Exception):
    pass


@contextmanager
def hold() -> Iterator[psycopg.Connection]:
    """Turn maintenance on for the duration. Yields the lock's connection, which is the one
    connection end_other_sessions leaves alone. Raises AlreadyInProgress if it is already on."""
    with _connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s, %s)", LOCK).fetchone()[0]:
            raise AlreadyInProgress("Another rollback is already running.")
        # Closing the connection, on the way out, releases the lock.
        yield conn


def end_other_sessions(lock: psycopg.Connection) -> int:
    """Close every other session on the app's database, so nothing still in a request or a task
    holds a table the restore must drop. Their requests fail; new ones get the maintenance page.
    Close Django's own connection in this thread first. Returns how many were ended."""
    rows = lock.execute(
        """
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
        WHERE datname = current_database() AND pid <> pg_backend_pid()
          AND backend_type = 'client backend'
        """
    ).fetchall()
    return sum(1 for (ended,) in rows if ended)
