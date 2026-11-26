"""Database backups: schedules kept in step with Hatchet's crons, dumps, and retention.

A schedule with a cron expression has a Hatchet cron trigger that runs the `backup-database`
task with its id. "Back up now" records a queued backup and hands it to the same task. The task
calls `run_backup`, which runs pg_dump, keeps the file where the schedule says (storage.py:
the backup folder or Amazon S3) and then deletes what the
schedule no longer keeps.
"""

import hashlib
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from . import maintenance, storage
from .models import Backup, BackupSchedule, Destination, Restore

logger = logging.getLogger(__name__)

Status = Backup.Status

NOT_QUEUED = "Could not hand the backup to the worker. Is Hatchet running?"


# --- seams the tests replace -----------------------------------------------------------------


def _after_commit(func) -> None:
    transaction.on_commit(func)


def _enqueue(schedule_id: int | None, backup_id: int) -> None:
    # Imported here: tasks.py builds the Hatchet client, which needs a token to exist.
    from .tasks import BackupInput, backup_database

    backup_database.run_no_wait(BackupInput(schedule_id=schedule_id, backup_id=backup_id))


class HatchetCrons:
    """Hatchet's cron triggers for the backup task."""

    def create(self, schedule: BackupSchedule) -> str:
        from .tasks import BackupInput, backup_database

        cron = backup_database.create_cron(
            cron_name=cron_name(schedule),
            expression=schedule.cron,
            input=BackupInput(schedule_id=schedule.pk),
        )
        return cron.metadata.id

    def delete(self, cron_id: str) -> None:
        from hatchet_sdk.clients.rest.exceptions import NotFoundException

        from foundation.jobs.hatchet import get_hatchet

        try:
            get_hatchet().cron.delete(cron_id)
        except NotFoundException:
            pass  # Already gone, which is what we wanted.

    def ids(self) -> set[str]:
        """Every cron trigger of the backup task."""
        from foundation.jobs.hatchet import get_hatchet

        from .tasks import backup_database

        crons = get_hatchet().cron.list(workflow_name=backup_database.name, limit=1000)
        return {cron.metadata.id for cron in crons.rows or []}


def _crons() -> HatchetCrons:
    return HatchetCrons()


def cron_name(schedule: BackupSchedule) -> str:
    return f"backup-schedule-{schedule.pk}"


# --- schedules -------------------------------------------------------------------------------


def save_schedule(schedule: BackupSchedule) -> BackupSchedule:
    """Save a schedule and, once that commits, bring its Hatchet cron in step with it."""
    schedule.full_clean()
    schedule.save()
    _after_commit(lambda: sync_schedule(schedule.pk))
    return schedule


def delete_schedule(schedule: BackupSchedule) -> None:
    """Delete a schedule and its Hatchet cron. Its backups stay, without a schedule."""
    cron_id = schedule.hatchet_cron_id
    schedule.delete()
    if cron_id:
        _after_commit(lambda: _delete_cron(cron_id))


def _delete_cron(cron_id: str) -> None:
    try:
        _crons().delete(cron_id)
    except Exception:
        logger.exception("Could not delete Hatchet cron %s; run sync_backup_schedules", cron_id)


def sync_schedule(schedule_id: int) -> bool:
    """Replace the schedule's Hatchet cron with one for its current expression (or none, when it
    is off or has no expression). Records what went wrong on the schedule; True when in step."""
    schedule = BackupSchedule.objects.filter(pk=schedule_id).first()
    if schedule is None:
        return False
    crons = _crons()
    cron_id = schedule.hatchet_cron_id
    try:
        if cron_id:
            crons.delete(cron_id)
            cron_id = ""
        if schedule.wants_cron:
            cron_id = crons.create(schedule)
        error = ""
    except Exception as exc:
        logger.exception("Could not sync backup schedule %s with Hatchet", schedule_id)
        error = f"Could not update Hatchet's cron: {exc}"
    BackupSchedule.objects.filter(pk=schedule_id).update(hatchet_cron_id=cron_id, sync_error=error)
    return not error


def sync_all() -> tuple[int, int]:
    """Make Hatchet's crons for the backup task exactly the enabled schedules': remove strays
    (from deleted schedules, or a failed delete) and recreate every schedule's own. Returns
    (schedules in step, schedules that failed)."""
    crons = _crons()
    for cron_id in crons.ids():
        crons.delete(cron_id)
    BackupSchedule.objects.update(hatchet_cron_id="")
    results = [sync_schedule(pk) for pk in BackupSchedule.objects.values_list("pk", flat=True)]
    return results.count(True), results.count(False)


# --- backing up ------------------------------------------------------------------------------


def back_up_now(schedule: BackupSchedule) -> Backup:
    """Queue a backup for this schedule now, whatever its cron. It counts towards the schedule's
    retention like any other."""
    backup = Backup.objects.create(
        schedule=schedule, trigger=Backup.Trigger.MANUAL, storage=schedule.destination
    )
    _after_commit(lambda: _queue(schedule.pk, backup.pk))
    return backup


def back_up_once(destination: str) -> Backup:
    """Queue a one-off backup, belonging to no schedule: no retention deletes it, so it is kept
    until someone deletes it."""
    if destination == Destination.S3 and not storage.s3_configured():
        raise ValidationError("Amazon S3 isn't set up: set AWS_S3_BACKUP_BUCKET.")
    backup = Backup.objects.create(trigger=Backup.Trigger.MANUAL, storage=destination)
    _after_commit(lambda: _queue(None, backup.pk))
    return backup


def _queue(schedule_id: int | None, backup_id: int) -> None:
    try:
        _enqueue(schedule_id, backup_id)
    except Exception:
        logger.exception("Could not queue backup %s", backup_id)
        Backup.objects.filter(pk=backup_id, status=Status.QUEUED).update(
            status=Status.FAILED, error=NOT_QUEUED, finished_at=timezone.now()
        )


def run_backup(schedule_id: int | None, backup_id: int | None = None) -> str:
    """The worker's part: dump the database, then apply the schedule's retention.

    With `backup_id`, runs that queued backup ("Back up now", or a one-off with no schedule),
    and nothing if it already started: a retry or a duplicate. Without, it is the schedule's
    cron firing, which does nothing for a schedule since turned off or deleted.
    """
    now = timezone.now()
    if backup_id is not None:
        claimed = Backup.objects.filter(pk=backup_id, schedule_id=schedule_id, status=Status.QUEUED)
        if not claimed.update(status=Status.RUNNING, started_at=now):
            return "skipped"
        backup = Backup.objects.select_related("schedule").get(pk=backup_id)
        schedule = backup.schedule
    else:
        schedule = BackupSchedule.objects.filter(pk=schedule_id).first()
        if schedule is None:
            return "no schedule"
        if not schedule.wants_cron:
            return "schedule off"
        backup = Backup.objects.create(
            schedule=schedule,
            trigger=Backup.Trigger.CRON,
            storage=schedule.destination,
            status=Status.RUNNING,
            started_at=now,
        )

    folder = f"schedule-{schedule.pk}" if schedule else "one-off"
    if not _make_dump(backup, folder=folder):
        return "failed"
    if schedule:
        apply_retention(schedule)
    return "completed"


def current_schema() -> str:
    """A fingerprint of the migrations applied to the database."""
    applied = sorted(
        f"{app}.{name}" for app, name in MigrationRecorder(connection).applied_migrations()
    )
    return hashlib.sha256("\n".join(applied).encode()).hexdigest()


def _make_dump(backup: Backup, folder: str) -> bool:
    """Dump the database for a running backup into its store, and settle it. True if it
    completed. The dump is made in the folder's staging area, then moved to the store."""
    stamp = f"{timezone.localtime(backup.started_at or timezone.now()):%Y%m%d-%H%M%S}"
    key = f"{folder}/{settings.BACKUP_FILE_PREFIX}-{stamp}-{backup.pk}.dump"
    staged = storage.staging_dir() / Path(key).name
    try:
        backup.schema = current_schema()
        backup.size = _dump(staged)
        storage.store(backup.storage).save(staged, key)
    except Exception as exc:
        staged.unlink(missing_ok=True)
        backup.status, backup.error, backup.finished_at = Status.FAILED, str(exc), timezone.now()
        backup.save(update_fields=["status", "error", "finished_at"])
        return False
    backup.status, backup.path, backup.finished_at = Status.COMPLETED, key, timezone.now()
    backup.save(update_fields=["status", "path", "size", "schema", "finished_at"])
    return True


def _connection_args() -> tuple[list[str], dict[str, str]]:
    """pg_dump/pg_restore arguments for the app's database, and an environment carrying its
    password: never on the command line."""
    db = settings.DATABASES["default"]
    args = []
    if db.get("HOST"):
        args += ["--host", db["HOST"]]
    if db.get("PORT"):
        args += ["--port", str(db["PORT"])]
    if db.get("USER"):
        args += ["--username", db["USER"]]
    args += ["--dbname", db["NAME"]]
    return args, {**os.environ, "PGPASSWORD": db.get("PASSWORD") or ""}


def _run(program: str, setting: str, args: list[str], env: dict) -> str:
    try:
        result = subprocess.run([program, *args], env=env, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"{Path(program).name} not found at “{program}”. Install the Postgres client tools "
            f"and set {setting} to its path."
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{program} exited with {result.returncode}")
    return result.stdout


def _dump(target: Path) -> int:
    """pg_dump the app's database into `target` (custom format, compressed; restore it with
    pg_restore). Writes to a .partial file first, so a file at `target` is always whole.
    Returns its size in bytes."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    connect, env = _connection_args()
    args = ["--format=custom", "--no-owner", "--no-privileges", f"--file={partial}", *connect]
    try:
        _run(settings.PG_DUMP, "PG_DUMP", args, env)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    partial.rename(target)
    return target.stat().st_size


def apply_retention(schedule: BackupSchedule) -> int:
    """Delete the backups the schedule no longer keeps. Returns how many went."""
    expired = list(Backup.objects.filter(schedule=schedule).beyond(schedule.keep))
    for backup in expired:
        delete_backup(backup)
    return len(expired)


def delete_backup(backup: Backup) -> None:
    """Delete a backup and, once that commits, its file, wherever it is kept."""
    path, kept_in = backup.path, backup.storage
    backup.delete()
    if path:
        _after_commit(lambda: _delete_file(kept_in, path))


def _delete_file(kept_in: str, path: str) -> None:
    try:
        storage.store(kept_in).delete(path)
    except Exception:
        # The row is gone either way; a file left behind is logged, not fatal.
        logger.exception("Could not delete backup file %s (%s)", path, kept_in)


# --- rolling back ----------------------------------------------------------------------------

# Every entry of the backups app's own tables, which a rollback leaves as they are: the list of
# backups must keep matching the files on disk, and the rollback's own record must survive it.
OWN_TABLES = re.compile(r"\bbackups_\w+")


def check_restorable(backup: Backup | None) -> Backup:
    """The backup, if the database can be rolled back to it now; ValidationError says why not."""
    if backup is None:
        raise ValidationError("That backup has been deleted.")
    if backup.status != Status.COMPLETED or not backup.path:
        raise ValidationError("Only a completed backup can be rolled back to.")
    try:
        kept = storage.store(backup.storage)
        found = kept.exists(backup.path)
    except storage.NotConfigured as error:
        raise ValidationError(str(error)) from error
    except Exception as error:
        raise ValidationError(f"Could not reach the backup's file: {error}") from error
    if not found:
        raise ValidationError(f"The backup's file is missing: {kept.describe(backup.path)}")
    if backup.schema != current_schema():
        raise ValidationError(
            "The database has had migrations since this backup was made, so its tables are "
            "shaped differently. Only a backup from the current schema can be rolled back to."
        )
    return backup


def request_restore(backup: Backup, requested_by: str) -> Restore:
    """Queue a rollback to `backup`. Refused while another is queued or running."""
    check_restorable(backup)
    if Restore.objects.in_flight().exists():
        raise ValidationError("A rollback is already queued or running.")
    restore = Restore.objects.create(
        backup=backup, backup_label=str(backup), requested_by=requested_by
    )
    _after_commit(lambda: _queue_restore(restore.pk))
    return restore


def _enqueue_restore(restore_id: int) -> None:
    from .tasks import RestoreInput, restore_database

    restore_database.run_no_wait(RestoreInput(restore_id=restore_id))


def _queue_restore(restore_id: int) -> None:
    try:
        _enqueue_restore(restore_id)
    except Exception:
        logger.exception("Could not queue rollback %s", restore_id)
        Restore.objects.filter(pk=restore_id, status=Restore.Status.QUEUED).update(
            status=Restore.Status.FAILED, error=NOT_QUEUED, finished_at=timezone.now()
        )


def run_restore(restore_id: int) -> str:
    """The worker's part of a rollback.

    0. Bring the backup's file down from S3, if that's where it is, while the app is still up.
    1. Turn maintenance on (maintenance.hold): the API answers 503 from here on.
    2. End every other session on the database, so nothing is mid-write.
    3. Take a safety backup of the database as it is, so the rollback can be undone.
    4. pg_restore the chosen backup in one transaction, leaving the backups app's tables out:
       if anything fails, Postgres rolls it all back and the database is as it was.
    5. Turn maintenance off.
    """
    RestoreStatus = Restore.Status
    claimed = Restore.objects.filter(pk=restore_id, status=RestoreStatus.QUEUED)
    if not claimed.update(status=RestoreStatus.RUNNING, started_at=timezone.now()):
        return "skipped"
    restore = Restore.objects.get(pk=restore_id)
    try:
        backup = check_restorable(restore.backup)
        with (
            storage.store(backup.storage).fetch(backup.path) as file,
            maintenance.hold() as lock,
        ):
            restore.safety_backup = _safety_backup(lock)
            restore.save(update_fields=["safety_backup"])
            connection.close()
            maintenance.end_other_sessions(lock)
            _restore(file)
    except Exception as exc:
        logger.exception("Rollback %s failed", restore_id)
        error = "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        Restore.objects.filter(pk=restore_id).update(
            status=RestoreStatus.FAILED, error=error, finished_at=timezone.now()
        )
        return "failed"
    Restore.objects.filter(pk=restore_id).update(
        status=RestoreStatus.COMPLETED, finished_at=timezone.now()
    )
    return "completed"


def _safety_backup(lock) -> Backup:
    # Django's connection in this thread is one of the sessions ended; it reconnects after.
    connection.close()
    maintenance.end_other_sessions(lock)
    # Kept in the folder: it is there for undoing this rollback, quickly, without the network.
    safety = Backup.objects.create(
        trigger=Backup.Trigger.SAFETY,
        storage=Backup.Storage.LOCAL,
        status=Status.RUNNING,
        started_at=timezone.now(),
    )
    if not _make_dump(safety, folder="before-rollback"):
        safety.refresh_from_db()
        raise RuntimeError(f"Could not back up the database before rolling back: {safety.error}")
    return safety


def _restore(file: Path) -> None:
    """pg_restore `file` over the app's database, in a single transaction, leaving out the
    backups app's own tables."""
    connect, env = _connection_args()
    listing = _run(settings.PG_RESTORE, "PG_RESTORE", ["--list", str(file)], env)
    kept = [line for line in listing.splitlines() if not OWN_TABLES.search(line)]
    with tempfile.NamedTemporaryFile("w", suffix=".list") as entries:
        entries.write("\n".join(kept) + "\n")
        entries.flush()
        args = [
            "--clean", "--if-exists", "--single-transaction", "--exit-on-error",
            "--no-owner", "--no-privileges", f"--use-list={entries.name}", *connect, str(file),
        ]  # fmt: skip
        _run(settings.PG_RESTORE, "PG_RESTORE", args, env)
