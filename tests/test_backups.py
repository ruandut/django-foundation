"""Spec 0013: database backups, scheduled by cron through Hatchet or run by hand."""

import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import Client

from foundation.backups import services
from foundation.backups.models import Backup, BackupSchedule
from tests.conftest import TEST_PASSWORD, make_user

Status = Backup.Status


class FakeCrons:
    """Hatchet's cron triggers, in memory."""

    def __init__(self):
        self.crons: dict[str, str] = {}  # id -> expression
        self.fail = False
        self.made = 0

    def create(self, schedule):
        if self.fail:
            raise RuntimeError("Hatchet is down")
        self.made += 1
        cron_id = f"cron-{self.made}"
        self.crons[cron_id] = schedule.cron
        return cron_id

    def delete(self, cron_id):
        if self.fail:
            raise RuntimeError("Hatchet is down")
        self.crons.pop(cron_id, None)

    def ids(self):
        return set(self.crons)


@pytest.fixture(autouse=True)
def backup_root(settings, tmp_path):
    settings.BACKUP_ROOT = tmp_path / "backups"
    return settings.BACKUP_ROOT


@pytest.fixture
def crons(db, monkeypatch):
    fake = FakeCrons()
    monkeypatch.setattr(services, "_crons", lambda: fake)
    monkeypatch.setattr(services, "_after_commit", lambda func: func())
    return fake


@pytest.fixture
def queued(monkeypatch):
    """Backups handed to the worker: (schedule_id, backup_id) pairs."""
    calls = []
    monkeypatch.setattr(services, "_enqueue", lambda *ids: calls.append(ids))
    return calls


@pytest.fixture
def fake_dump(monkeypatch):
    """pg_dump stand-in: writes a small file, or fails when told to."""
    state = {"fail": None}

    def dump(target: Path) -> int:
        if state["fail"]:
            raise RuntimeError(state["fail"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dump")
        return 4

    monkeypatch.setattr(services, "_dump", dump)
    return state


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


def schedule(crons, name="Nightly", cron="0 0 * * *", keep=3, enabled=True):
    return services.save_schedule(BackupSchedule(name=name, cron=cron, keep=keep, enabled=enabled))


def backups_of(s):
    return list(Backup.objects.filter(schedule=s).order_by("pk"))


# --- schedules and Hatchet's crons -----------------------------------------------------------


def test_at_0013_01_a_schedule_with_a_cron_gets_a_hatchet_cron(crons):
    s = schedule(crons)
    s.refresh_from_db()
    assert crons.crons == {s.hatchet_cron_id: "0 0 * * *"}
    assert s.sync_error == ""


def test_at_0013_02_changing_the_cron_replaces_hatchets(crons):
    s = schedule(crons)
    s.refresh_from_db()
    s.cron = "30 1 * * 0"
    services.save_schedule(s)
    s.refresh_from_db()
    assert crons.crons == {s.hatchet_cron_id: "30 1 * * 0"}


def test_at_0013_03_turning_off_or_clearing_the_cron_removes_it(crons):
    off = schedule(crons, name="Off")
    off.refresh_from_db()
    off.enabled = False
    services.save_schedule(off)
    by_hand = schedule(crons, name="By hand", cron="")
    assert crons.crons == {}
    for s in (off, by_hand):
        s.refresh_from_db()
        assert s.hatchet_cron_id == "" and s.sync_error == ""


def test_at_0013_04_deleting_a_schedule_removes_its_cron_and_keeps_its_backups(crons, fake_dump):
    s = schedule(crons)
    services.run_backup(s.pk)
    s.refresh_from_db()
    services.delete_schedule(s)
    assert crons.crons == {}
    [backup] = Backup.objects.all()
    assert backup.schedule is None and backup.status == Status.COMPLETED


@pytest.mark.parametrize(
    "cron", ["nonsense", "0 0 * *", "60 0 * * *", "0 24 * * *", "0 0 32 * *", "0 0 * 13 *"]
)
def test_at_0013_05_a_bad_cron_is_refused(crons, cron):
    with pytest.raises(ValidationError):
        schedule(crons, cron=cron)
    assert not BackupSchedule.objects.exists() and crons.crons == {}


def test_at_0013_05_good_crons_are_accepted(crons):
    for n, cron in enumerate(["*/15 * * * *", "0 2 * * 1-5", "0 0 1,15 * *", "@daily"]):
        schedule(crons, name=f"s{n}", cron=cron)
    assert len(crons.crons) == 4


def test_at_0013_05_at_least_one_backup_is_kept(crons):
    with pytest.raises(ValidationError):
        schedule(crons, keep=0)


def test_at_0013_06_when_hatchet_cant_be_reached_the_schedule_says_so(crons):
    crons.fail = True
    s = schedule(crons)
    s.refresh_from_db()
    assert "Hatchet is down" in s.sync_error and s.hatchet_cron_id == ""
    crons.fail = False
    assert services.sync_schedule(s.pk)
    s.refresh_from_db()
    assert s.sync_error == "" and s.hatchet_cron_id in crons.crons


def test_at_0013_07_sync_all_puts_hatchet_in_step(crons):
    a = schedule(crons, name="A")
    schedule(crons, name="B", enabled=False)
    crons.crons["stray"] = "0 3 * * *"  # from a schedule deleted while Hatchet was down
    assert services.sync_all() == (2, 0)
    a.refresh_from_db()
    assert crons.crons == {a.hatchet_cron_id: "0 0 * * *"}


# --- backing up ------------------------------------------------------------------------------


def test_at_0013_08_the_cron_firing_makes_a_backup(crons, fake_dump, backup_root):
    s = schedule(crons)
    assert services.run_backup(s.pk) == "completed"
    [backup] = backups_of(s)
    assert backup.trigger == Backup.Trigger.CRON and backup.status == Status.COMPLETED
    assert backup.size == 4 and (backup_root / backup.path).read_bytes() == b"dump"
    assert backup.started_at and backup.finished_at


def test_at_0013_09_a_cron_for_a_schedule_turned_off_does_nothing(crons, fake_dump):
    s = schedule(crons)
    s.enabled = False
    services.save_schedule(s)
    assert services.run_backup(s.pk) == "schedule off"
    assert services.run_backup(s.pk + 100) == "no schedule"
    assert not Backup.objects.exists()


def test_at_0013_10_back_up_now_from_admin(crons, queued, fake_dump, admin_client):
    s = schedule(crons, cron="", enabled=False)
    response = admin_client.post(
        "/admin/backups/backupschedule/",
        {"action": "back_up_now", "_selected_action": [s.pk]},
        follow=True,
    )
    assert response.status_code == 200
    [backup] = backups_of(s)
    assert backup.status == Status.QUEUED and backup.trigger == Backup.Trigger.MANUAL
    assert queued == [(s.pk, backup.pk)]
    # The worker's part; a second delivery of the same backup does nothing.
    assert services.run_backup(s.pk, backup.pk) == "completed"
    assert services.run_backup(s.pk, backup.pk) == "skipped"
    backup.refresh_from_db()
    assert backup.status == Status.COMPLETED


def test_at_0013_11_a_backup_that_cant_be_queued_fails(crons, monkeypatch):
    def down(*ids):
        raise RuntimeError("no Hatchet")

    monkeypatch.setattr(services, "_enqueue", down)
    backup = services.back_up_now(schedule(crons))
    backup.refresh_from_db()
    assert backup.status == Status.FAILED and backup.error == services.NOT_QUEUED


def test_at_0013_12_a_failed_dump_is_recorded_and_keeps_the_old_backups(
    crons, fake_dump, backup_root
):
    s = schedule(crons, keep=1)
    services.run_backup(s.pk)
    [good] = backups_of(s)
    fake_dump["fail"] = "connection refused"
    assert services.run_backup(s.pk) == "failed"
    _, failed = backups_of(s)
    assert failed.status == Status.FAILED and failed.error == "connection refused"
    assert failed.path == "" and (backup_root / good.path).exists()


# --- retention -------------------------------------------------------------------------------


def test_at_0013_13_only_the_newest_backups_are_kept(crons, fake_dump, backup_root):
    s = schedule(crons, keep=2)
    for _ in range(4):
        services.run_backup(s.pk)
    kept = backups_of(s)
    assert len(kept) == 2
    files = sorted(p.relative_to(backup_root) for p in backup_root.rglob("*.dump"))
    assert files == sorted(Path(b.path) for b in kept)
    assert kept[-1].pk == Backup.objects.order_by("-pk").first().pk


def test_at_0013_14_retention_counts_each_schedule_apart(crons, fake_dump):
    a, b = schedule(crons, name="A", keep=1), schedule(crons, name="B", keep=3)
    for _ in range(3):
        services.run_backup(a.pk)
        services.run_backup(b.pk)
    assert (len(backups_of(a)), len(backups_of(b))) == (1, 3)


def test_at_0013_15_failures_go_with_the_backups_they_are_older_than(crons, fake_dump):
    s = schedule(crons, keep=1)
    fake_dump["fail"] = "boom"
    services.run_backup(s.pk)
    services.run_backup(s.pk)
    assert len(backups_of(s)) == 2  # no good backup yet: failures stay readable
    fake_dump["fail"] = None
    services.run_backup(s.pk)
    [only] = backups_of(s)
    assert only.status == Status.COMPLETED


def test_at_0013_15_queued_backups_are_never_pruned(crons, queued, fake_dump):
    s = schedule(crons, keep=1)
    waiting = services.back_up_now(s)
    services.run_backup(s.pk)
    services.run_backup(s.pk)
    assert waiting.pk in [b.pk for b in backups_of(s)]


def test_at_0013_16_deleting_a_backup_in_admin_deletes_its_file(
    crons, fake_dump, backup_root, admin_client
):
    s = schedule(crons)
    services.run_backup(s.pk)
    [backup] = backups_of(s)
    file = backup_root / backup.path
    response = admin_client.post(f"/admin/backups/backup/{backup.pk}/delete/", {"post": "yes"})
    assert response.status_code == 302
    assert not Backup.objects.exists() and not file.exists()


# --- who may ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["/admin/backups/backupschedule/", "/admin/backups/backup/", "/admin/backups/"]
)
def test_at_0013_17_only_superusers_see_backups(db, url):
    staff = make_user("staff@example.com")
    staff.is_staff = True
    staff.save()
    # Even with every backup permission granted.
    from django.contrib.auth.models import Permission

    staff.user_permissions.set(Permission.objects.filter(content_type__app_label="backups"))
    client = Client()
    client.force_login(staff)
    assert client.get(url).status_code in (403, 404)


def test_at_0013_17_superusers_see_the_pages(admin_client, crons):
    schedule(crons)
    for url in ["/admin/backups/backupschedule/", "/admin/backups/backup/"]:
        assert admin_client.get(url).status_code == 200
    assert admin_client.get("/admin/backups/backupschedule/add/").status_code == 200


# --- a real dump -----------------------------------------------------------------------------


def _pg_tool(name: str) -> str | None:
    dump = shutil.which(settings.PG_DUMP)
    if dump is None:
        return None
    return dump if name == "pg_dump" else shutil.which(str(Path(dump).with_name(name)))


@pytest.mark.skipif(_pg_tool("pg_dump") is None, reason="pg_dump isn't installed (PG_DUMP)")
@pytest.mark.django_db(transaction=True)
def test_at_0013_18_the_dump_is_a_restorable_pg_dump(crons, backup_root):
    s = schedule(crons)
    assert services.run_backup(s.pk) == "completed"
    [backup] = backups_of(s)
    file = backup_root / backup.path
    assert backup.size == file.stat().st_size > 0
    assert not list(backup_root.rglob("*.partial"))
    pg_restore = _pg_tool("pg_restore")
    if pg_restore:
        listing = subprocess.run([pg_restore, "--list", file], capture_output=True, text=True)
        assert listing.returncode == 0
        assert "backups_backupschedule" in listing.stdout


# --- the "Back up now" button on the Backups list (0013) -------------------------------------


def test_at_0013_19_the_backups_list_has_a_back_up_now_button(crons, admin_client):
    schedule(crons, name="Nightly")
    page = admin_client.get("/admin/backups/backup/").content.decode()
    assert 'value="Back up now"' in page
    assert "Nightly (This server&#x27;s backup folder, keeps 3)" in page
    assert "One-off, to this server" in page
    assert "Amazon S3" not in page.split('id="back-up-target"')[1].split("</select>")[0]


def test_at_0013_20_backing_up_a_schedule_from_the_list(crons, queued, fake_dump, admin_client):
    s = schedule(crons)
    response = admin_client.post(
        "/admin/backups/backup/back-up-now/", {"target": f"schedule:{s.pk}"}, follow=True
    )
    assert response.redirect_chain[-1][0] == "/admin/backups/backup/"
    assert "Backup queued" in response.content.decode()
    [backup] = backups_of(s)
    assert queued == [(s.pk, backup.pk)] and backup.trigger == Backup.Trigger.MANUAL


def test_at_0013_21_a_one_off_backup_is_kept_until_deleted(
    crons, queued, fake_dump, admin_client, backup_root
):
    response = admin_client.post("/admin/backups/backup/back-up-now/", {"target": "once:local"})
    assert response.status_code == 302
    [backup] = Backup.objects.all()
    assert backup.schedule is None and backup.storage == "local"
    assert queued == [(None, backup.pk)]
    assert services.run_backup(None, backup.pk) == "completed"
    backup.refresh_from_db()
    assert backup.path.startswith("one-off/") and (backup_root / backup.path).is_file()
    # A schedule's retention never touches it.
    s = schedule(crons, keep=1)
    services.run_backup(s.pk)
    services.run_backup(s.pk)
    assert Backup.objects.filter(pk=backup.pk).exists()


@pytest.mark.parametrize("target", ["", "schedule:999", "schedule:x", "once:ftp", "once:s3"])
def test_at_0013_22_back_up_now_refuses_what_it_cant_do(crons, queued, admin_client, target):
    response = admin_client.post(
        "/admin/backups/backup/back-up-now/", {"target": target}, follow=True
    )
    assert response.status_code == 200 and "Backup queued" not in response.content.decode()
    assert not Backup.objects.exists() and queued == []


def test_at_0013_23_back_up_now_is_for_superusers_and_posts_only(crons, queued, admin_client):
    staff = make_user("staff@example.com")
    staff.is_staff = True
    staff.save()
    client = Client()
    client.force_login(staff)
    assert (
        client.post("/admin/backups/backup/back-up-now/", {"target": "once:local"}).status_code
        == 403
    )
    assert admin_client.get("/admin/backups/backup/back-up-now/").status_code == 405
    assert not Backup.objects.exists()
