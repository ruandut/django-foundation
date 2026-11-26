from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the Hatchet worker with every task found in the project's tasks.py (TASKS lists)"

    def add_arguments(self, parser):
        parser.add_argument("--slots", type=int, default=5, help="Concurrent task slots")

    def handle(self, *args, slots, **options):
        # Running as a management command means Django is set up, so tasks can use the ORM.
        from foundation.jobs.hatchet import get_hatchet
        from foundation.jobs.registry import discover_tasks

        tasks = discover_tasks()
        self.stdout.write(f"Registering {len(tasks)} tasks: " + ", ".join(t.name for t in tasks))
        name = getattr(settings, "HATCHET_WORKER_NAME", "worker")
        get_hatchet().worker(name, slots=slots, workflows=tasks).start()
