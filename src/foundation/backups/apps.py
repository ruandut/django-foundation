from django.apps import AppConfig
from django.conf import settings
from django.core import checks

# Set by foundation.settings.backups(); listed so a project that skipped it hears about it.
SETTINGS = [
    "BACKUP_ROOT",
    "BACKUP_S3",
    "BACKUP_FILE_PREFIX",
    "PG_DUMP",
    "PG_RESTORE",
    "MAINTENANCE_MESSAGE",
]


class BackupsConfig(AppConfig):
    name = "foundation.backups"
    # "backups", not "foundation_backups": eden-tracker's tables and migrations already use it.
    label = "backups"
    verbose_name = "Database backups"

    def ready(self):
        checks.register(check_settings)


def check_settings(app_configs, **kwargs):
    missing = [name for name in SETTINGS if not hasattr(settings, name)]
    if not missing:
        return []
    return [
        checks.Error(
            f"foundation.backups needs {', '.join(missing)}.",
            hint="Add globals().update(foundation.settings.backups(env, ...)) to settings.py.",
            id="foundation.E001",
        )
    ]
