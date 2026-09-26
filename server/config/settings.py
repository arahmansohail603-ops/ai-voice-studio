import json
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_local_env()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def env_int_min(name: str, default: int, minimum: int) -> int:
    value = env_int(name, default)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def env_list(name: str, default: str = "") -> list[str]:
    value = os.environ.get(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def env_json(name: str, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON") from exc


def database_config() -> dict:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme in {"sqlite", "sqlite3"}:
            name = unquote(parsed.path or "")
            name = name.removeprefix("/")
            if not name:
                name = str(BASE_DIR / "db.sqlite3")
            elif not Path(name).is_absolute() and name != ":memory:":
                name = str(BASE_DIR / name)
            return {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": name,
            }
        engine_names = {
            "postgres": "django.db.backends.postgresql",
            "postgresql": "django.db.backends.postgresql",
            "mysql": "django.db.backends.mysql",
        }
        engine = engine_names.get(parsed.scheme, f"django.db.backends.{parsed.scheme}")
        query = parse_qs(parsed.query)
        return {
            "ENGINE": engine,
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "OPTIONS": {
                key: value[-1]
                for key, value in query.items()
                if key in {"sslmode", "connect_timeout"}
            },
        }
    engine = env("DATABASE_ENGINE", "django.db.backends.sqlite3")
    name = env("DATABASE_NAME", str(BASE_DIR / "db.sqlite3"))
    if engine.endswith("sqlite3") and name != ":memory:":
        database_path = Path(name).expanduser()
        if not database_path.is_absolute():
            database_path = BASE_DIR / database_path
        name = str(database_path)
    return {
        "ENGINE": engine,
        "NAME": name,
        "USER": env("DATABASE_USER"),
        "PASSWORD": env("DATABASE_PASSWORD"),
        "HOST": env("DATABASE_HOST"),
        "PORT": env("DATABASE_PORT"),
    }


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = env("DJANGO_SECRET_KEY", "local-development-only-django-secret")
_PLACEHOLDER_SECRETS = {
    "local-development-only-django-secret",
    "replace-with-a-long-random-value",
    "replace-with-a-long-random-server-secret",
    "local-development-only-hmac-pepper-change-me",
}
if not DEBUG and (len(SECRET_KEY) < 50 or SECRET_KEY in _PLACEHOLDER_SECRETS):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a unique random value of at least 50 characters "
        "when DJANGO_DEBUG is disabled"
    )
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "licenses",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
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
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": database_config()}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("DJANGO_DATA_UPLOAD_MAX_MEMORY_SIZE", 64 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env_int("DJANGO_DATA_UPLOAD_MAX_NUMBER_FIELDS", 100)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_HSTS_SECONDS = env_int("DJANGO_SECURE_HSTS_SECONDS", 0 if DEBUG else 31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG
)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https")
    if env_bool("DJANGO_TRUST_PROXY_SSL_HEADER", False)
    else None
)

CACHES = {
    "default": {
        "BACKEND": env(
            "LICENSE_CACHE_BACKEND",
            "django.core.cache.backends.locmem.LocMemCache",
        ),
        "LOCATION": env("LICENSE_CACHE_LOCATION", "license-server"),
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "licenses.exceptions.api_exception_handler",
    "DEFAULT_THROTTLE_RATES": {
        "license_activation": env("LICENSE_RATE_LIMIT_ACTIVATION", "10/minute"),
        "license_status": env("LICENSE_RATE_LIMIT_STATUS", "60/minute"),
        "health": env("LICENSE_RATE_LIMIT_HEALTH", "120/minute"),
    },
    "UNAUTHENTICATED_USER": None,
}

LICENSE_HMAC_PEPPER = env(
    "LICENSE_HMAC_PEPPER",
    "local-development-only-hmac-pepper-change-me" if DEBUG else "",
)
LICENSE_LEASE_DURATION_SECONDS = env_int_min("LICENSE_LEASE_DURATION_SECONDS", 3600, 1)
LICENSE_LEASE_GRACE_SECONDS = env_int_min("LICENSE_LEASE_GRACE_SECONDS", 300, 0)
LICENSE_NONCE_TTL_SECONDS = env_int_min("LICENSE_NONCE_TTL_SECONDS", 900, 1)
LICENSE_IDEMPOTENCY_TTL_SECONDS = env_int_min(
    "LICENSE_IDEMPOTENCY_TTL_SECONDS", 86400, 1
)
LICENSE_LEASE_RETENTION_COUNT = env_int_min("LICENSE_LEASE_RETENTION_COUNT", 10, 1)
LICENSE_MAX_METADATA_BYTES = env_int_min("LICENSE_MAX_METADATA_BYTES", 16 * 1024, 1)
LICENSE_TRUST_PROXY_HEADERS = env_bool("LICENSE_TRUST_PROXY_HEADERS", False)
LICENSE_SIGNING_KEY_ID = env("LICENSE_SIGNING_KEY_ID", "local-dev")
LICENSE_SIGNING_KEYS = env_json("LICENSE_SIGNING_KEYS", {})
if not LICENSE_SIGNING_KEYS:
    singular_key = env("LICENSE_SIGNING_PRIVATE_KEY", "")
    if singular_key:
        LICENSE_SIGNING_KEYS = {LICENSE_SIGNING_KEY_ID: singular_key}
if not DEBUG and (
    not LICENSE_HMAC_PEPPER
    or len(LICENSE_HMAC_PEPPER) < 32
    or LICENSE_HMAC_PEPPER in _PLACEHOLDER_SECRETS
    or "replace-with" in LICENSE_HMAC_PEPPER.lower()
):
    raise ImproperlyConfigured(
        "LICENSE_HMAC_PEPPER must be a unique random value of at least 32 characters"
    )
if not DEBUG and not LICENSE_SIGNING_KEYS:
    raise ImproperlyConfigured("LICENSE_SIGNING_KEYS must be configured in production")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        }
    },
    "loggers": {
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
