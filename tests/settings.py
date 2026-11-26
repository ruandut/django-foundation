"""A minimal project for the package's tests. Needs a Postgres server: DATABASE_URL, by default
the rent-tracker dev database's server (make up there), where pytest makes its own test_ one."""

import environ

from foundation import settings as foundation

env = environ.Env()

SECRET_KEY = "tests-only"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost"]
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://rent:rent@localhost:5434/foundation")
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
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    *foundation.SECURITY_MIDDLEWARE,
    *foundation.BACKUPS_MIDDLEWARE,
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
