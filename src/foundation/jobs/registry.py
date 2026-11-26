"""Find every Hatchet task in the project.

Convention: a Django app that owns background work has `tasks.py` (or a `tasks/` package)
exposing `TASKS = [...]`. The worker registers all of them, so apps keep their tasks next to
their models instead of in one central jobs module. Only apps whose module starts with one of
settings.HATCHET_TASK_APPS are searched (the project's own `apps.` and this package's
`foundation.`), so third-party apps' modules never get imported by accident.
"""

from importlib import import_module
from importlib.util import find_spec

from django.apps import apps
from django.conf import settings

DEFAULT_PREFIXES = ("apps.", "foundation.")


def discover_tasks() -> list:
    prefixes = tuple(getattr(settings, "HATCHET_TASK_APPS", DEFAULT_PREFIXES))
    found = []
    for config in apps.get_app_configs():
        if not config.name.startswith(prefixes):
            continue
        module_name = f"{config.name}.tasks"
        if find_spec(module_name) is None:
            continue
        # Import errors inside a tasks module should fail loudly, not be skipped.
        found.extend(getattr(import_module(module_name), "TASKS", []))
    return found
