"""Spec 0015: where backups go, this server's folder or Amazon S3 (mocked by moto)."""

import os
import shutil
from pathlib import Path

import boto3
import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection
from moto import mock_aws

from foundation.backups import services, storage
from foundation.backups.models import Backup, BackupSchedule, Destination

Status = Backup.Status
BUCKET = "test-backups"
PREFIX = "tests/backups/"


class _NoCrons:
    def create(self, schedule):
        return "cron"

    def delete(self, cron_id):
        pass

    def ids(self):
        return set()


@pytest.fixture(autouse=True)
def inline(db, settings, tmp_path, monkeypatch):
    settings.BACKUP_ROOT = tmp_path / "backups"
    monkeypatch.setattr(services, "_after_commit", lambda func: func())
    monkeypatch.setattr(services, "_crons", lambda: _NoCrons())
    monkeypatch.setattr(services, "_enqueue_restore", lambda restore_id: None)
    # Never hand anything to a real Hatchet: tests run the worker's part themselves.
    monkeypatch.setattr(services, "_enqueue", lambda *ids: None)


@pytest.fixture
def s3(settings, monkeypatch):
    """A mocked AWS account with an empty bucket, set up in settings as the environment would."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with mock_aws():
        storage._s3_client.cache_clear()
        settings.BACKUP_S3 = {
            "bucket": BUCKET, "prefix": PREFIX, "region": "af-south-1", "endpoint_url": None,
            "access_key_id": "testing", "secret_access_key": "testing",
        }  # fmt: skip
        client = boto3.client("s3", region_name="af-south-1")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "af-south-1"}
        )
        yield client
        storage._s3_client.cache_clear()


@pytest.fixture
def fake_dump(monkeypatch):
    def dump(target: Path) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dump")
        return 4

    monkeypatch.setattr(services, "_dump", dump)


def objects(client) -> list[str]:
    return sorted(o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def schedule(destination=Destination.LOCAL, keep=3, name="Nightly"):
    return services.save_schedule(
        BackupSchedule(name=name, cron="", keep=keep, destination=destination)
    )


@pytest.mark.skipif(bool(os.environ.get("BACKUP_ROOT")), reason="BACKUP_ROOT is set")
def test_at_0015_01_by_default_backups_go_in_a_folder_inside_media_root():
    from tests import settings as config  # as loaded, before any test changed it

    assert config.BACKUP_ROOT == Path(config.MEDIA_ROOT) / "backups"
    assert BackupSchedule().destination == Destination.LOCAL


def test_at_0015_02_a_local_backup_lands_in_the_folder(fake_dump):
    s = schedule()
    services.back_up_now(s)
    services.run_backup(s.pk, Backup.objects.get().pk)
    backup = Backup.objects.get()
    assert backup.storage == "local" and backup.status == Status.COMPLETED
    assert (Path(settings.BACKUP_ROOT) / backup.path).read_bytes() == b"dump"
    assert not list((Path(settings.BACKUP_ROOT) / ".staging").iterdir())


def test_at_0015_03_s3_needs_setting_up_first():
    with pytest.raises(ValidationError) as error:
        schedule(Destination.S3)
    assert "AWS_S3_BACKUP_BUCKET" in str(error.value)
    assert not BackupSchedule.objects.exists()


def test_at_0015_04_an_s3_backup_goes_to_the_bucket_encrypted(s3, fake_dump):
    s = schedule(Destination.S3)
    services.back_up_now(s)
    services.run_backup(s.pk, Backup.objects.get().pk)
    backup = Backup.objects.get()
    assert backup.storage == "s3" and backup.status == Status.COMPLETED, backup.error
    assert objects(s3) == [PREFIX + backup.path]
    head = s3.head_object(Bucket=BUCKET, Key=PREFIX + backup.path)
    assert head["ServerSideEncryption"] == "AES256"
    # Nothing is left behind on this server.
    root = Path(settings.BACKUP_ROOT)
    assert [p for p in root.rglob("*") if p.is_file()] == []


def test_at_0015_05_retention_deletes_from_s3(s3, fake_dump):
    s = schedule(Destination.S3, keep=2)
    for _ in range(4):
        _by_hand(s)
    kept = Backup.objects.filter(schedule=s).order_by("pk")
    assert kept.count() == 2
    assert objects(s3) == sorted(PREFIX + b.path for b in kept)


def _by_hand(s):
    backup = services.back_up_now(s)
    assert services.run_backup(s.pk, backup.pk) == "completed"


def test_at_0015_06_deleting_an_s3_backup_deletes_the_object(s3, fake_dump):
    s = schedule(Destination.S3)
    _by_hand(s)
    services.delete_backup(Backup.objects.get())
    assert objects(s3) == []


def test_at_0015_07_when_s3_fails_the_backup_says_why(s3, fake_dump):
    s = schedule(Destination.S3)
    s3.delete_bucket(Bucket=BUCKET)
    backup = services.back_up_now(s)
    assert services.run_backup(s.pk, backup.pk) == "failed"
    backup.refresh_from_db()
    assert backup.status == Status.FAILED and "NoSuchBucket" in backup.error
    assert not list((Path(settings.BACKUP_ROOT) / ".staging").iterdir())


def test_at_0015_08_changing_destination_keeps_earlier_backups_where_they_are(s3, fake_dump):
    s = schedule(Destination.LOCAL)
    _by_hand(s)
    s.destination = Destination.S3
    services.save_schedule(s)
    _by_hand(s)
    first, second = Backup.objects.order_by("pk")
    assert (first.storage, second.storage) == ("local", "s3")
    assert (Path(settings.BACKUP_ROOT) / first.path).is_file()
    assert objects(s3) == [PREFIX + second.path]
    for backup in (first, second):
        services.check_restorable(backup)


def test_at_0015_09_an_s3_backup_missing_from_the_bucket_cant_be_rolled_back_to(s3, fake_dump):
    s = schedule(Destination.S3)
    _by_hand(s)
    backup = Backup.objects.get()
    s3.delete_object(Bucket=BUCKET, Key=PREFIX + backup.path)
    with pytest.raises(ValidationError, match="missing"):
        services.check_restorable(backup)


def _have_pg_tools() -> bool:
    return bool(shutil.which(settings.PG_DUMP) and shutil.which(settings.PG_RESTORE))


@pytest.mark.skipif(not _have_pg_tools(), reason="pg_dump/pg_restore aren't installed")
@pytest.mark.django_db(transaction=True)
def test_at_0015_10_rolling_back_to_an_s3_backup(s3):
    Group.objects.create(name="Before")
    s = schedule(Destination.S3)
    _by_hand(s)
    backup = Backup.objects.get()
    Group.objects.create(name="After")

    restore = services.request_restore(backup, "root@example.com")
    assert services.run_restore(restore.pk) == "completed"
    connection.close()
    assert list(Group.objects.values_list("name", flat=True)) == ["Before"]
    # The download was only for the restore; the safety backup stays in the folder.
    restore.refresh_from_db()
    assert restore.safety_backup.storage == "local"
    root = Path(settings.BACKUP_ROOT)
    assert [p.relative_to(root).parts[0] for p in root.rglob("*.dump")] == ["before-rollback"]


def test_at_0015_11_a_one_off_backup_can_go_to_s3(s3, fake_dump):
    backup = services.back_up_once(Destination.S3)
    assert services.run_backup(None, backup.pk) == "completed"
    backup.refresh_from_db()
    assert backup.storage == "s3" and objects(s3) == [PREFIX + backup.path]


def test_at_0015_12_without_keys_s3_uses_the_machines_own_credentials(s3, fake_dump):
    # No keys set: boto3 finds credentials through its own chain. On EC2 that is the
    # instance role; here moto's fake environment keys stand in for it.
    assert storage.store("s3").client._request_signer._credentials.method == "explicit"
    settings.BACKUP_S3 = {**settings.BACKUP_S3, "access_key_id": None, "secret_access_key": None}
    storage._s3_client.cache_clear()

    backup = services.back_up_once(Destination.S3)
    assert services.run_backup(None, backup.pk) == "completed"
    backup.refresh_from_db()
    assert objects(s3) == [PREFIX + backup.path]

    credentials = storage.store("s3").client._request_signer._credentials
    assert credentials.method != "explicit" and credentials.access_key != "testing"
