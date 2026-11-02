import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_list(name, default=""):
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


DEBUG = os.getenv("PGDAS_DEBUG", "0") == "1"

SECRET_KEY = os.getenv("PGDAS_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("Set PGDAS_SECRET_KEY (see .env.example) or run with PGDAS_DEBUG=1 for local development.")
    SECRET_KEY = "insecure-development-key-do-not-use-in-production"

ALLOWED_HOSTS = env_list("PGDAS_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("PGDAS_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "audit",
    "servers",
    "explorer",
    "backups",
    "monitor",
    "watch",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "pgdas.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "pgdas.wsgi.application"

# PGDAS keeps its own data (users, saved servers, audit log, sessions) in one SQLite file, so installing it needs no
# database server. The PostgreSQL servers it browses are separate and are added in the app.
SQLITE_PATH = Path(os.getenv("PGDAS_SQLITE_PATH") or BASE_DIR / "data" / "pgdas.sqlite3")
SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": SQLITE_PATH,
        "OPTIONS": {
            # WAL lets readers and the one writer work together; IMMEDIATE makes a writer wait for its turn
            # (up to `timeout` seconds) instead of failing with "database is locked".
            "timeout": 20,
            "init_command": "PRAGMA journal_mode=WAL;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}

if sys.argv[1:2] == ["test"]:
    # Django's real password hashing is slow on purpose (about a second per user). Tests create many users, so
    # `manage.py test` uses a fast hasher. This applies only to the test command and never to a running server.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage" if not DEBUG
                    else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
if not DEBUG:
    SESSION_COOKIE_SECURE = os.getenv("PGDAS_SECURE_COOKIES", "1") == "1"
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE

# Optional dedicated Fernet key for saved server passwords; otherwise derived from SECRET_KEY.
ENCRYPTION_KEY = os.getenv("PGDAS_ENCRYPTION_KEY", "")

# Backups: pg_dump files go here; PGDAS_PG_BIN is the folder holding pg_dump and pg_restore (blank = look on PATH).
BACKUP_DIR = Path(os.getenv("PGDAS_BACKUP_DIR") or BASE_DIR / "data" / "backups")
PG_BIN = os.getenv("PGDAS_PG_BIN", "")

# Size trends and alerts (the Watch page). Samples are kept this many days; the thresholds decide when an alert shows.
WATCH_KEEP_DAYS = int(os.getenv("PGDAS_WATCH_KEEP_DAYS", "90"))
WATCH_BLOCKED_SECONDS = int(os.getenv("PGDAS_WATCH_BLOCKED_SECONDS", "30"))          # a session waiting on a lock this long
WATCH_IDLE_TX_SECONDS = int(os.getenv("PGDAS_WATCH_IDLE_TX_SECONDS", "300"))         # idle in a transaction this long
WATCH_SLOW_SECONDS = int(os.getenv("PGDAS_WATCH_SLOW_SECONDS", "300"))               # a query running this long
WATCH_CONNECTIONS_PERCENT = int(os.getenv("PGDAS_WATCH_CONNECTIONS_PERCENT", "80"))  # share of max_connections in use
WATCH_GROWTH_PERCENT = int(os.getenv("PGDAS_WATCH_GROWTH_PERCENT", "20"))            # growth over 7 days ...
WATCH_GROWTH_MIN_BYTES = int(os.getenv("PGDAS_WATCH_GROWTH_MIN_BYTES", str(100 * 1024 * 1024)))   # ... and at least this much

# Sending alerts (run_watch): a webhook (Slack, Teams, Discord, ...) and/or email. Both are off until set.
ALERT_WEBHOOK_URL = os.getenv("PGDAS_ALERT_WEBHOOK_URL", "")
ALERT_EMAIL_TO = env_list("PGDAS_ALERT_EMAIL_TO")
ALERT_EMAIL_FROM = os.getenv("PGDAS_ALERT_EMAIL_FROM", "PGDAS <pgdas@localhost>")
ALERT_REPEAT_HOURS = int(os.getenv("PGDAS_ALERT_REPEAT_HOURS", "12"))              # remind about a critical alert this often; 0 = never
EMAIL_HOST = os.getenv("PGDAS_EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("PGDAS_EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("PGDAS_EMAIL_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("PGDAS_EMAIL_PASSWORD", "")
EMAIL_USE_SSL = os.getenv("PGDAS_EMAIL_SSL", "0") == "1"                           # port 465 style
EMAIL_USE_TLS = os.getenv("PGDAS_EMAIL_TLS", "1") == "1" and not EMAIL_USE_SSL     # STARTTLS, port 587 style
EMAIL_TIMEOUT = 15

# SQL runner safety limits.
STATEMENT_TIMEOUT_MS = int(os.getenv("PGDAS_STATEMENT_TIMEOUT_MS", "30000"))
MAX_ROWS = int(os.getenv("PGDAS_MAX_ROWS", "1000"))
# A delete must fit in the audit log (every deleted row is saved there as JSON, so it can be re-inserted).
MAX_DELETE_ROWS = int(os.getenv("PGDAS_MAX_DELETE_ROWS", "10000"))
MAX_UNDO_ROWS = int(os.getenv("PGDAS_MAX_UNDO_ROWS", "10000"))      # an UPDATE touching more rows than this cannot be undone
