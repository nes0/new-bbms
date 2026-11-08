"""
Django settings for BBMS (Build Brighton Member System).

Everything that differs between a laptop, staging and production comes from environment
variables (or a `.env` file in the project root when developing). See `.env.example`.
The defaults are safe for local development only.
"""

import base64
import hashlib
import sys
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    SECURE_SSL_REDIRECT=(bool, False),
)
# A .env file is optional; real deployments set environment variables instead.
environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env("DEBUG")
# True while `manage.py test` runs (the test runner turns DEBUG off, so code can't rely on it).
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

# Never use the development key in production: settings refuse to start without a real one.
SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise environ.ImproperlyConfigured("Set the SECRET_KEY environment variable (or DEBUG=True for development).")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # BBMS apps
    "apps.core",
    "apps.members",
    "apps.access",
    "apps.equipment",
    "apps.payments",
    "apps.legacy",
    "apps.site_settings",
    "apps.storage",
    "apps.billing",
    "apps.profiles",
    "apps.discord",
    "apps.notifications",
    "apps.proposals",
    "apps.expenses",
    "apps.finance",
    "oauth2_provider",
    "apps.moodle",
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

ROOT_URLCONF = "bbms.urls"

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
                "apps.core.context_processors.site",
                "apps.notifications.context_processors.unread",
            ],
        },
    },
]

WSGI_APPLICATION = "bbms.wsgi.application"

# Database: PostgreSQL in staging/production (DATABASE_URL=postgres://...),
# SQLite on a laptop if DATABASE_URL isn't set.
DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)

# Members log in with their email address (see apps/members/models.py).
AUTH_USER_MODEL = "members.Member"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "home"

# New passwords use Django's default (PBKDF2). Passwords migrated from the legacy system are
# bcrypt hashes; members can log in with them and are upgraded automatically (spec 08 §8.2).
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "apps.members.hashers.LegacyBcryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# Uploaded files (member photos): a private DigitalOcean Spaces bucket in production (spec 01 §1.8), never public-read;
# links to them are signed and expire. Without SPACES_BUCKET, files go in MEDIA_ROOT (development only: App
# Platform's disk is wiped on every deploy).
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
SPACES_BUCKET = env("SPACES_BUCKET", default="")
if SPACES_BUCKET:
    DEFAULT_STORAGE = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": SPACES_BUCKET,
            "endpoint_url": env("SPACES_ENDPOINT", default="https://lon1.digitaloceanspaces.com"),
            "region_name": env("SPACES_REGION", default="lon1"),
            "access_key": env("SPACES_KEY"),
            "secret_key": env("SPACES_SECRET"),
            "default_acl": "private",
            "querystring_auth": True,  # signed links
            "querystring_expire": 300,  # seconds
            "file_overwrite": False,
        },
    }
else:
    DEFAULT_STORAGE = {"BACKEND": "django.core.files.storage.FileSystemStorage"}
STORAGES = {
    "default": DEFAULT_STORAGE,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if DEBUG
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Email: printed to the console when developing; a real provider's SMTP server in production (spec 01 §1.8).
# Set EMAIL_HOST (and usually the user and password) and mail goes by SMTP; leave it unset and emails are only printed
# to the log, which `check --deploy` warns about. Port 587 with STARTTLS is the usual; for a provider that wants
# port 465, set EMAIL_PORT=465 and EMAIL_USE_SSL=True (the two encryptions can't be used together).
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_BACKEND = env(
    "EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend"
    if EMAIL_HOST
    else "django.core.mail.backends.console.EmailBackend",
)
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")  # a secret: set it as an encrypted variable
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=not EMAIL_USE_SSL)
# A slow mail server must not hold up a web request or a nightly job for long.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="BBMS <noreply@bbms.buildbrighton.com>")

# The space's name is an admin setting (apps/site_settings, 02 §2.8), not an environment variable.
# The site's public address, for links that leave the site: emails, QR codes on device screens.
SITE_URL = env("SITE_URL", default="http://localhost:8000").rstrip("/")

# Keys that encrypt proofs of identity in the database (spec 03 §3.1), so backups and dumps hold only ciphertext.
# One or more Fernet keys, comma-separated: the first encrypts, all of them decrypt (for changing keys; see
# `manage.py rotate_id_document_key`). Make one with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Keep it only in the app's environment: never in the repo, the database or the shadow site (08). Without it,
# production refuses uploads. When developing, a key is worked out from SECRET_KEY instead.
ID_DOCUMENT_KEYS = env.list("ID_DOCUMENT_KEYS", default=[])
if not ID_DOCUMENT_KEYS and DEBUG:
    ID_DOCUMENT_KEYS = [
        base64.urlsafe_b64encode(hashlib.sha256(b"bbms id documents, development only:" + SECRET_KEY.encode()).digest())
    ]

# GoCardless (Direct Debit, spec 04 §4.2). Without a token, development and tests use a fake that sends nothing.
# Use a sandbox token everywhere but the live site; the shadow site gets a read-only one (08).
GOCARDLESS_ACCESS_TOKEN = env("GOCARDLESS_ACCESS_TOKEN", default="")
GOCARDLESS_ENVIRONMENT = env("GOCARDLESS_ENVIRONMENT", default="sandbox")  # or "live"
# Webhook endpoint secret, from GoCardless's dashboard (Developers → Webhooks). Webhooks are refused without it.
GOCARDLESS_WEBHOOK_SECRET = env("GOCARDLESS_WEBHOOK_SECRET", default="")

# Stripe (card payments, spec 04 §4.3). Without a key, development and tests use a fake that sends nothing.
# Use a test-mode key (sk_test_...) everywhere but the live site.
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")
# The webhook endpoint's signing secret (whsec_...), from Stripe's dashboard. Webhooks are refused without it.
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# Discord (spec 06 §6.14): members link their Discord account, and BBMS adds them to the server and keeps their
# member / former-member role right. From the Discord developer portal (the application's OAuth2 and Bot pages) and
# the server (with Developer Mode on, right-click → Copy ID). Without them, development and tests use a fake
# (/dev/discord/) and a live site doesn't ask members to link Discord.
DISCORD_CLIENT_ID = env("DISCORD_CLIENT_ID", default="")
DISCORD_CLIENT_SECRET = env("DISCORD_CLIENT_SECRET", default="")
DISCORD_BOT_TOKEN = env("DISCORD_BOT_TOKEN", default="")
DISCORD_GUILD_ID = env("DISCORD_GUILD_ID", default="")  # the server
DISCORD_MEMBER_ROLE_ID = env("DISCORD_MEMBER_ROLE_ID", default="")  # given while someone is a member
DISCORD_FORMER_MEMBER_ROLE_ID = env("DISCORD_FORMER_MEMBER_ROLE_ID", default="")  # given once they've left
# Where BBMS posts the welcome message (its text is in the admin's Settings). Empty: no welcome message.
DISCORD_WELCOME_CHANNEL_ID = env("DISCORD_WELCOME_CHANNEL_ID", default="")
DISCORD_ANNOUNCEMENT_CHANNEL_ID = env("DISCORD_ANNOUNCEMENT_CHANNEL_ID", default="")  # proposals (06 §6.3)

# PayPal (subscriptions and credit top-ups, spec 04 §4.4; not built yet). The space's PayPal account: every PayPal
# button pays it, and IPNs for any other account are refused. Old accounts, during a switch to a new one: their
# payments are still recorded, but members are never asked to pay them.
PAYPAL_ACCOUNT_EMAIL = env("PAYPAL_ACCOUNT_EMAIL", default="")
PAYPAL_OLD_ACCOUNT_EMAILS = env.list("PAYPAL_OLD_ACCOUNT_EMAILS", default=[])
PAYPAL_SANDBOX = env.bool("PAYPAL_SANDBOX", default=True)  # False only on the live site

# Moodle (spec 06 §6.12). Its address is an admin setting. The web-service token lets BBMS ask Moodle whether a member
# has finished a course (a dedicated Moodle account with the least rights needed). Without it, development and tests use
# a fake, and a live site can't check courses.
MOODLE_TOKEN = env("MOODLE_TOKEN", default="")
# "Log in with BBMS": BBMS is an OpenID Connect provider for Moodle. Tokens are signed with this RSA private key (PEM;
# in an environment variable write each line break as \n). Without it the provider is switched off.
OIDC_RSA_PRIVATE_KEY = env("OIDC_RSA_PRIVATE_KEY", default="").replace("\\n", "\n")
OAUTH2_PROVIDER = {
    "OIDC_ENABLED": bool(OIDC_RSA_PRIVATE_KEY),
    "OIDC_RSA_PRIVATE_KEY": OIDC_RSA_PRIVATE_KEY,
    "OIDC_ISS_ENDPOINT": f"{SITE_URL}/o",
    "SCOPES": {"openid": "Know who you are", "profile": "Your name", "email": "Your email address"},
    "OAUTH2_VALIDATOR_CLASS": "apps.moodle.oidc.BBMSValidator",
    # Moodle's generic OAuth 2 login doesn't send PKCE. Its client has a secret, which is what protects the code.
    "PKCE_REQUIRED": False,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 600,
    "ID_TOKEN_EXPIRE_SECONDS": 600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 3600,
}

# Security settings for production (behind DigitalOcean App Platform's HTTPS proxy).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env("SECURE_SSL_REDIRECT")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
# HSTS: browsers always use HTTPS for this site. Starts at 1 hour in production; raise it once settled.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0 if DEBUG else 3600)
# Deliberately not applied to other buildbrighton.com subdomains (e.g. Moodle), so silence those two hints.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
}
