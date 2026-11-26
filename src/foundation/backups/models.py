"""Scheduled dumps of the whole database (pg_dump), run by the Hatchet worker.

A dump holds everything in the database, so only superusers see these, in Django admin.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from hatchet_sdk.features.cron import CreateCronTriggerConfig

from .querysets import BackupQuerySet, BackupScheduleQuerySet, RestoreQuerySet


def validate_cron(expression: str) -> None:
    """Hatchet's own check, plus the ranges of each of the five fields."""
    if not expression:
        return
    try:
        CreateCronTriggerConfig(expression=expression)
    except ValueError as error:
        raise ValidationError("Not a cron expression: minute hour day month weekday.") from error
    if expression.startswith("@"):
        return
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for part, (low, high) in zip(expression.split()[-5:], ranges, strict=True):
        for number in part.replace("*/", " ").replace("-", " ").replace(",", " ").split():
            if number != "*" and not low <= int(number) <= high:
                raise ValidationError(f"{number} is out of range in “{part}” ({low}–{high}).")


class Destination(models.TextChoices):
    LOCAL = "local", "This server's backup folder"
    S3 = "s3", "Amazon S3"


class BackupSchedule(models.Model):
    name = models.CharField(max_length=100, unique=True)
    cron = models.CharField(
        max_length=100,
        blank=True,
        validators=[validate_cron],
        help_text=(
            "minute hour day month weekday, in UTC: “0 0 * * *” runs at midnight UTC "
            "every night. @daily, @weekly and the like work too. Leave it empty for a "
            "schedule you only run by hand, with “Back up now”."
        ),
    )
    keep = models.PositiveIntegerField(
        default=7,
        validators=[MinValueValidator(1)],
        help_text="How many completed backups of this schedule to keep. Older ones are deleted, "
        "files and all, after each new one completes.",
    )
    destination = models.CharField(
        max_length=10,
        choices=Destination.choices,
        default=Destination.LOCAL,
        help_text="Where its backups go. The folder is BACKUP_ROOT (a backups folder inside "
        "MEDIA_ROOT unless set); Amazon S3 is the bucket in AWS_S3_BACKUP_BUCKET. Changing it "
        "applies to new backups; earlier ones stay where they are.",
    )
    enabled = models.BooleanField(
        default=True, help_text="Off stops the cron. “Back up now” still works."
    )
    # Hatchet's cron trigger for this schedule, kept in step by services.sync_schedule.
    hatchet_cron_id = models.CharField(max_length=64, blank=True, editable=False)
    sync_error = models.TextField(blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = BackupScheduleQuerySet.as_manager()

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        from .storage import s3_configured

        if self.destination == Destination.S3 and not s3_configured():
            raise ValidationError(
                {"destination": "Amazon S3 isn't set up: set AWS_S3_BACKUP_BUCKET."}
            )

    @property
    def wants_cron(self) -> bool:
        return self.enabled and bool(self.cron)


class Backup(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    class Trigger(models.TextChoices):
        CRON = "cron"
        MANUAL = "manual", "By hand"
        # Taken just before a rollback, so the rollback can itself be undone. Not a schedule's,
        # so no retention deletes it.
        SAFETY = "safety", "Before a rollback"

    Storage = Destination

    # Kept when its schedule is deleted, so deleting a schedule never deletes a backup.
    schedule = models.ForeignKey(
        BackupSchedule, null=True, on_delete=models.SET_NULL, related_name="backups"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    trigger = models.CharField(max_length=10, choices=Trigger.choices)
    # Where its file is kept, and its key there: relative to BACKUP_ROOT, or under the S3 prefix.
    storage = models.CharField(
        max_length=10, choices=Destination.choices, default=Destination.LOCAL
    )
    path = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    # Fingerprint of the migrations applied when it was made. A rollback needs the database to
    # have the same ones, so a dump never lands on tables shaped differently.
    schema = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = BackupQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.schedule or 'Backup'} at {self.created_at:%Y-%m-%d %H:%M}"


class Restore(models.Model):
    """A rollback of the database to a backup.

    The backups app's own tables are left out of every rollback (see services.run_restore), so
    these rows, the schedules and the list of backups survive it. For the same reason nothing
    here has a foreign key to a table a rollback replaces: who asked is kept as their email.
    """

    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        COMPLETED = "completed"
        FAILED = "failed"

    backup = models.ForeignKey(
        Backup, null=True, on_delete=models.SET_NULL, related_name="restores"
    )
    # What it was rolled back to, kept in words in case that backup is later deleted.
    backup_label = models.CharField(max_length=200)
    safety_backup = models.ForeignKey(
        Backup, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_by = models.CharField(max_length=254)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = RestoreQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "rollback"

    def __str__(self) -> str:
        return f"Rollback to {self.backup_label}"
