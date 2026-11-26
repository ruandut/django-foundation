"""Spec 0014: rolling the database back to a backup, with the app down for maintenance."""

import shutil
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client

from foundation.backups import maintenance, services
from foundation.backups.models import Backup, BackupSchedule, Restore
from tests.conftest import TEST_PASSWORD, make_user

Status = Backup.Status
RStatus = Restore.Status


@pytest.fixture(autouse=True)
def backup_root(settings, tmp_path):
    settings.BACKUP_ROOT = tmp_path / "backups"
    return settings.BACKUP_ROOT


@pytest.fixture
def inline(db, monkeypatch):
    """Commit hooks run at once; queued rollbacks are collected instead of sent to Hatchet."""
    calls = []
    monkeypatch.setattr(services, "_after_commit", lambda func: func())
    monkeypatch.setattr(services, "_enqueue_restore", calls.append)
    monkeypatch.setattr(services, "_crons", lambda: _NoCrons())
    return calls


class _NoCrons:
    def create(self, schedule):
        return "cron"

    def delete(self, cron_id):
        pass

    def ids(self):
        return set()


def completed_backup(backup_root, schema=None) -> Backup:
    """A completed backup with a (fake) file on disk."""
    backup = Backup.objects.create(
        trigger=Backup.Trigger.MANUAL,
        status=Status.COMPLETED,
        path="by-hand/tests.dump",
        size=4,
        schema=schema if schema is not None else services.current_schema(),
    )
    (backup_root / "by-hand").mkdir(parents=True, exist_ok=True)
    (backup_root / backup.path).write_bytes(b"dump")
    return backup


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_superuser(
        "root", email="root@example.com", password=TEST_PASSWORD
    )


@pytest.fixture
def admin_client(superuser):
    client = Client()
    client.force_login(superuser)
    return client


# --- choosing a backup -----------------------------------------------------------------------


def test_at_0014_01_backups_and_rollbacks_are_listed_in_admin(admin_client, backup_root, inline):
    backup = completed_backup(backup_root)
    services.request_restore(backup, "root@example.com")
    page = admin_client.get("/admin/backups/backup/").content.decode()
    assert "Roll the database back to this backup" in page
    assert admin_client.get("/admin/backups/restore/").status_code == 200


def test_at_0014_02_rolling_back_asks_first(admin_client, backup_root, inline):
    backup = completed_backup(backup_root)
    data = {"action": "roll_back", "_selected_action": [backup.pk]}
    page = admin_client.post("/admin/backups/backup/", data)
    assert page.status_code == 200 and "Yes, roll back" in page.content.decode()
    assert not Restore.objects.exists() and inline == []

    admin_client.post("/admin/backups/backup/", {**data, "confirmed": "yes"})
    [restore] = Restore.objects.all()
    assert restore.backup == backup and restore.requested_by == "root@example.com"
    assert restore.status == RStatus.QUEUED and inline == [restore.pk]


def test_at_0014_03_one_backup_at_a_time(admin_client, backup_root, inline):
    a, b = completed_backup(backup_root), completed_backup(backup_root)
    data = {"action": "roll_back", "_selected_action": [a.pk, b.pk], "confirmed": "yes"}
    admin_client.post("/admin/backups/backup/", data)
    assert not Restore.objects.exists()


@pytest.mark.parametrize("problem", ["failed", "no file", "other schema", "deleted"])
def test_at_0014_04_only_a_whole_backup_of_this_schema_can_be_rolled_back_to(
    backup_root, inline, problem
):
    backup = completed_backup(backup_root)
    if problem == "failed":
        backup.status = Status.FAILED
    elif problem == "no file":
        (backup_root / backup.path).unlink()
    elif problem == "other schema":
        backup.schema = "0" * 64
    backup.save()
    with pytest.raises(ValidationError):
        services.check_restorable(None if problem == "deleted" else backup)
    if problem != "deleted":
        with pytest.raises(ValidationError):
            services.request_restore(backup, "root@example.com")
    assert not Restore.objects.exists()


def test_at_0014_05_one_rollback_at_a_time(backup_root, inline):
    backup = completed_backup(backup_root)
    services.request_restore(backup, "root@example.com")
    with pytest.raises(ValidationError):
        services.request_restore(backup, "root@example.com")


def test_at_0014_06_only_superusers_can_roll_back(db, backup_root, inline):
    backup = completed_backup(backup_root)
    staff = make_user("staff@example.com")
    staff.is_staff = True
    staff.save()
    client = Client()
    client.force_login(staff)
    data = {"action": "roll_back", "_selected_action": [backup.pk], "confirmed": "yes"}
    assert client.post("/admin/backups/backup/", data).status_code in (403, 404)
    assert client.get("/admin/backups/restore/").status_code in (403, 404)
    assert not Restore.objects.exists()


def test_at_0014_07_a_rollback_that_cant_be_queued_fails(backup_root, inline, monkeypatch):
    def down(restore_id):
        raise RuntimeError("no Hatchet")

    monkeypatch.setattr(services, "_enqueue_restore", down)
    restore = services.request_restore(completed_backup(backup_root), "root@example.com")
    restore.refresh_from_db()
    assert restore.status == RStatus.FAILED and restore.error == services.NOT_QUEUED


# --- maintenance -----------------------------------------------------------------------------


def test_at_0014_08_while_a_rollback_runs_the_api_answers_503(db, api, settings):
    assert api.get("/api/health/").status_code == 200
    with maintenance.hold():
        assert maintenance.in_progress()
        for response in [api.get("/api/health/"), api.get("/api/units/")]:
            assert response.status_code == 503
            assert response["Retry-After"] == str(maintenance.RETRY_AFTER_SECONDS)
            assert response.json() == {
                "code": "maintenance",
                "detail": settings.MAINTENANCE_MESSAGE,
            }
        # Sign-in too, and Django admin gets a page saying so.
        assert api.post("/api/auth/token/", {}, format="json").status_code == 503
        admin = Client().get("/admin/")
        assert admin.status_code == 503 and b"Down for maintenance" in admin.content
    assert not maintenance.in_progress()
    assert api.get("/api/health/").status_code == 200


def test_at_0014_09_two_rollbacks_cant_hold_maintenance_at_once(db):
    with maintenance.hold(), pytest.raises(maintenance.AlreadyInProgress):
        with maintenance.hold():
            pass


def test_at_0014_10_a_rollback_while_another_runs_fails(backup_root, inline):
    restore = services.request_restore(completed_backup(backup_root), "root@example.com")
    with maintenance.hold():
        assert services.run_restore(restore.pk) == "failed"
    restore.refresh_from_db()
    assert "already running" in restore.error
    assert services.run_restore(restore.pk) == "skipped"  # never run twice


# A rollback ends every other session, this test's own included: these need real transactions.
@pytest.mark.django_db(transaction=True)
def test_at_0014_11_a_failed_rollback_turns_maintenance_off(backup_root, inline, monkeypatch):
    restore = services.request_restore(completed_backup(backup_root), "root@example.com")

    def broken(file):
        raise RuntimeError("pg_restore: error: could not read")

    monkeypatch.setattr(services, "_restore", broken)
    monkeypatch.setattr(services, "_dump", _fake_dump)
    assert services.run_restore(restore.pk) == "failed"
    restore.refresh_from_db()
    assert restore.status == RStatus.FAILED and "could not read" in restore.error
    assert not maintenance.in_progress()


def _fake_dump(target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"safe")
    return 4


@pytest.mark.django_db(transaction=True)
def test_at_0014_12_no_safety_backup_no_rollback(backup_root, inline, monkeypatch):
    restore = services.request_restore(completed_backup(backup_root), "root@example.com")
    restored = []
    monkeypatch.setattr(services, "_restore", restored.append)

    def broken(target):
        raise RuntimeError("disk full")

    monkeypatch.setattr(services, "_dump", broken)
    assert services.run_restore(restore.pk) == "failed"
    restore.refresh_from_db()
    assert "disk full" in restore.error and restored == []


# --- the real thing --------------------------------------------------------------------------


def _have_pg_tools() -> bool:
    dump = shutil.which(settings.PG_DUMP)
    return bool(dump and shutil.which(settings.PG_RESTORE))


@pytest.mark.skipif(not _have_pg_tools(), reason="pg_dump/pg_restore aren't installed")
@pytest.mark.django_db(transaction=True)
def test_at_0014_13_rolling_back_restores_the_data_and_keeps_the_backups(backup_root, inline):
    owner = make_user("owner@example.com")
    realm = Group.objects.create(name="Before")
    schedule = BackupSchedule.objects.create(name="Nightly", cron="", keep=5)
    first = Backup.objects.create(
        schedule=schedule, trigger=Backup.Trigger.MANUAL, status=Status.RUNNING
    )
    assert services._make_dump(first, folder="test")

    # Life goes on after the backup.
    Group.objects.filter(pk=realm.pk).update(name="After")
    Group.objects.create(name="Made later")
    owner.delete()

    restore = services.request_restore(first, "root@example.com")
    assert services.run_restore(restore.pk) == "completed"
    connection.close()

    # The data is as it was...
    assert sorted(Group.objects.values_list("name", flat=True)) == ["Before"]
    assert get_user_model().objects.filter(email="owner@example.com").exists()
    # ...the backups app's own tables are as they are now...
    restore.refresh_from_db()
    assert restore.status == RStatus.COMPLETED and restore.finished_at
    assert BackupSchedule.objects.filter(pk=schedule.pk).exists()
    # ...and a safety backup of the database as it was just before can undo it.
    safety = restore.safety_backup
    assert safety.trigger == Backup.Trigger.SAFETY and safety.status == Status.COMPLETED
    assert (backup_root / safety.path).is_file() and safety.schema == first.schema
    assert not maintenance.in_progress()

    undo = services.request_restore(safety, "root@example.com")
    assert services.run_restore(undo.pk) == "completed"
    connection.close()
    assert sorted(Group.objects.values_list("name", flat=True)) == ["After", "Made later"]
    assert Restore.objects.count() == 2
