from datetime import timedelta

from django.db import close_old_connections
from hatchet_sdk import Context
from pydantic import BaseModel

from foundation.jobs.hatchet import get_hatchet

hatchet = get_hatchet()


class BackupInput(BaseModel):
    # None for a one-off backup, which belongs to no schedule.
    schedule_id: int | None = None
    # Set for "Back up now", which records the backup before queueing it. A cron run has none.
    backup_id: int | None = None


@hatchet.task(
    name="backup-database",
    input_validator=BackupInput,
    # Waits for a free worker for a while rather than dropping a backup when the worker is busy.
    schedule_timeout=timedelta(hours=1),
    execution_timeout=timedelta(hours=2),
)
def backup_database(input: BackupInput, ctx: Context) -> dict:
    """Dump the database for a schedule, then apply its retention (services.run_backup).
    Fired by the schedule's Hatchet cron, or queued by "Back up now"."""
    from .services import run_backup

    close_old_connections()
    try:
        return {"outcome": run_backup(input.schedule_id, input.backup_id)}
    finally:
        close_old_connections()


class RestoreInput(BaseModel):
    restore_id: int


@hatchet.task(
    name="restore-database",
    input_validator=RestoreInput,
    schedule_timeout=timedelta(hours=1),
    execution_timeout=timedelta(hours=2),
)
def restore_database(input: RestoreInput, ctx: Context) -> dict:
    """Roll the database back to a backup, with the app down for maintenance meanwhile
    (services.run_restore). Never retried: a rollback runs once, when someone asks for it."""
    from .services import run_restore

    close_old_connections()
    try:
        return {"outcome": run_restore(input.restore_id)}
    finally:
        close_old_connections()


TASKS = [backup_database, restore_database]
