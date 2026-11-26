from django.apps import AppConfig
from django.conf import settings
from django.core import checks

# Set by foundation.settings.mfa(); listed so a project that skipped it hears about it.
SETTINGS = [
    "MFA_ENCRYPTION_KEYS",
    "MFA_ADAPTER",
    "MFA_SETUP_TIMEOUT",
    "MFA_LOGIN_TIMEOUT",
    "MFA_MAX_FAILURES",
    "MFA_LOCKOUT_SECONDS",
]
NEEDS_APPS = ["allauth", "allauth.account", "allauth.mfa"]


class MFAConfig(AppConfig):
    name = "foundation.mfa"
    label = "foundation_mfa"
    verbose_name = "Two-factor sign-in"

    def ready(self):
        checks.register(check_settings)


def check_settings(app_configs, **kwargs):
    from django.apps import apps

    errors = []
    missing = [name for name in SETTINGS if not hasattr(settings, name)]
    if missing:
        errors.append(
            checks.Error(
                f"foundation.mfa needs {', '.join(missing)}.",
                hint="Add globals().update(foundation.settings.mfa(env, issuer=...)) to "
                "settings.py.",
                id="foundation.E002",
            )
        )
    absent = [name for name in NEEDS_APPS if not apps.is_installed(name)]
    if absent:
        errors.append(
            checks.Error(
                f"foundation.mfa needs {', '.join(absent)} in INSTALLED_APPS.",
                id="foundation.E003",
            )
        )
    return errors
