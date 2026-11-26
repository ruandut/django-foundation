"""Query logic for backup schedules and the backups they made.

Backups are of the whole database, so these are superusers only.
"""

from django.db import models


class BackupScheduleQuerySet(models.QuerySet):
    def on_cron(self):
        """Enabled schedules that have a cron expression: the ones Hatchet should be firing."""
        return self.filter(enabled=True).exclude(cron="")


class BackupQuerySet(models.QuerySet):
    def completed(self):
        return self.filter(status=self.model.Status.COMPLETED)

    def newest_first(self):
        return self.order_by("-created_at", "-id")

    def beyond(self, keep: int):
        """What retention removes when `keep` backups are to be kept: every completed backup
        after the newest `keep`, and failed ones older than the oldest completed one kept.
        Queued and running backups are never touched."""
        kept = list(self.completed().order_by("-pk").values_list("pk", flat=True)[:keep])
        if len(kept) < keep:
            # Fewer good backups than we keep: nothing goes, failures included, so their errors
            # stay readable.
            return self.none()
        settled = [self.model.Status.COMPLETED, self.model.Status.FAILED]
        return self.filter(status__in=settled, pk__lt=kept[-1])


class RestoreQuerySet(models.QuerySet):
    def in_flight(self):
        return self.filter(status__in=[self.model.Status.QUEUED, self.model.Status.RUNNING])
