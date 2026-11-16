import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=""):
    return os.environ.get(name, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "drf_api_logger",
    "apilogs",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "drf_api_logger.middleware.api_logger_middleware.APILoggerMiddleware",
]

if not DEBUG:  # serve admin static files from gunicorn
    MIDDLEWARE.insert(1, "whitenoise.middleware.WhiteNoiseMiddleware")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

# Django's own tables (users, sessions, permissions) live in SQLite.
# API logs live in Cassandra only -- see apilogs/cassandra_store.py.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
# Log partitions are keyed by the local date in this zone, so "today",
# "last 7 days" etc. line up with whole partitions. Pick it once and keep it.
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "EXCEPTION_HANDLER": "api.exceptions.json_exception_handler",
}

# --- drf-api-logger ----------------------------------------------------------
# No SQL storage: every API call fires API_LOGGER_SIGNAL, and
# apilogs/listener.py writes it to Cassandra asynchronously.
DRF_API_LOGGER_DATABASE = False
DRF_API_LOGGER_SIGNAL = True
DRF_API_LOGGER_PATH_TYPE = "FULL_PATH"
DRF_API_LOGGER_EXCLUDE_KEYS = ["password", "token", "access", "refresh", "secret"]
DRF_API_LOGGER_ENABLE_TRACING = True

# --- Cassandra ---------------------------------------------------------------
CASSANDRA = {
    "HOSTS": env("CASSANDRA_HOSTS", "127.0.0.1").split(","),
    "PORT": int(env("CASSANDRA_PORT", "9042")),
    "KEYSPACE": env("CASSANDRA_KEYSPACE", "api_logs"),
    "LOCAL_DC": env("CASSANDRA_LOCAL_DC", "datacenter1"),
    "REPLICATION": {
        "class": "SimpleStrategy",
        "replication_factor": int(env("CASSANDRA_REPLICATION_FACTOR", "1")),
    },
    "USER": env("CASSANDRA_USER"),
    "PASSWORD": env("CASSANDRA_PASSWORD"),
    # Log rows expire after this many days (0 = keep forever).
    "LOG_TTL_SECONDS": int(env("API_LOG_TTL_DAYS", "180")) * 86400,
    # After a failed connect, wait this long before trying again so a dead
    # cluster doesn't add a connect timeout to every API request.
    "RETRY_AFTER_SECONDS": 30,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {"cassandra": {"level": "WARNING"}},
}
