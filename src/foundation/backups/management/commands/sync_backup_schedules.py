from django.core.management.base import BaseCommand

from foundation.backups.services import sync_all


class Command(BaseCommand):
    help = (
        "Make Hatchet's backup crons match the backup schedules, e.g. after Hatchet's data was "
        "reset. Needs the worker to have started once, so Hatchet knows the backup task."
    )

    def handle(self, *args, **options):
        synced, failed = sync_all()
        self.stdout.write(f"{synced} schedules in step with Hatchet, {failed} failed.")
        if failed:
            self.stdout.write("See each schedule's sync error in Django admin.")
