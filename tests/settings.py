"""A minimal project for the package's tests. Needs a Postgres server, named by DATABASE_URL,
where pytest makes its own test_ database."""

import os

import environ

from foundation import settings as foundation

env = environ.Env()

SECRET_KEY = "tests-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {
    "default": env.db(
        "DATABASE_URL", default="postgres://postgres:postgres@localhost:5432/foundation"
    )
}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_altcha",
    "foundation.jobs",
    "foundation.security",
    "foundation.backups",
    "allauth",
    "allauth.account",
    "allauth.mfa",
    "foundation.mfa",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *foundation.SECURITY_MIDDLEWARE,
    *foundation.BACKUPS_MIDDLEWARE,
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    *foundation.MFA_MIDDLEWARE,
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "tests.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
STATIC_URL = "static/"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
MEDIA_ROOT = "/nonexistent-media"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    **foundation.rest_framework(env, debug=DEBUG),
}

globals().update(foundation.security(env, secret_key=SECRET_KEY))
globals().update(foundation.backups(env, media_root=MEDIA_ROOT, name="tests"))
# A fixed, invented Fernet key: tests only.
os.environ.setdefault("MFA_ENCRYPTION_KEY", "dGVzdHMtb25seS10ZXN0cy1vbmx5LXRlc3RzLW9ubHk=")
globals().update(foundation.mfa(env, issuer="Tests"))
