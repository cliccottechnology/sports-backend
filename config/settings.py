"""JETZ Sports Academy — Django settings (production-ready, env-driven)."""
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "False").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "apps.accounts",
    "apps.academy",
    "apps.students",
    "apps.parents",
    "apps.coaches",
    "apps.batches",
    "apps.sessions.apps.SessionsConfig",
    "apps.attendance",
    "apps.fees",
    "apps.payments",
    "apps.whatsapp",
    "apps.trainer_fees",
    "apps.reports",
    "apps.notifications",
    "apps.audit",
    "apps.documents",
    "apps.support",
    "apps.customization",
    "faceauth.apps.FaceauthConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
AUTH_USER_MODEL = "accounts.User"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "sports_db"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}


# DATABASES = {
#     "default": {
#         "ENGINE": "django.db.backends.postgresql",
#         "NAME": os.environ.get("DB_NAME", "jetz_db"),
#         "USER": os.environ.get("DB_USER", "jetz_user"),
#         "PASSWORD": os.environ.get("DB_PASSWORD", "change-me"),
#         "HOST": os.environ.get("DB_HOST", "localhost"),
#         "PORT": os.environ.get("DB_PORT", "5432"),
#     }
# }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / os.environ.get("MEDIA_ROOT", "media")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("apps.accounts.auth.ActiveJWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
        "apps.common.modules.ModuleEnabled",
    ),
    "DEFAULT_PAGINATION_CLASS": "apps.common.pagination.StandardPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.exceptions.api_exception_handler",
}

SPECTACULAR_SETTINGS = {"TITLE": "JETZ Sports Academy API", "VERSION": "1.0.0"}

# Session lifetimes.
#   ACCESS  — short-lived bearer token the apps silently refresh.
#   SESSION — absolute cap on one login; after it the user must sign in again.
#             Enforced by the `session_exp` claim (apps/accounts/auth.py) on the
#             access AND refresh paths, so rotation cannot extend past it.
#   REFRESH — must be >= SESSION, otherwise the refresh token itself dies first
#             and the session ends early. Defaults to SESSION. The legacy
#             `JWT_REFRESH_DAYS` key is still accepted.
JWT_ACCESS_MINUTES = int(os.environ.get("JWT_ACCESS_MINUTES", "15"))
JWT_SESSION_MINUTES = int(os.environ.get("JWT_SESSION_MINUTES", "720"))  # 12 hours
if os.environ.get("JWT_REFRESH_MINUTES"):
    JWT_REFRESH_MINUTES = int(os.environ["JWT_REFRESH_MINUTES"])
elif os.environ.get("JWT_REFRESH_DAYS"):
    JWT_REFRESH_MINUTES = int(os.environ["JWT_REFRESH_DAYS"]) * 24 * 60
else:
    JWT_REFRESH_MINUTES = JWT_SESSION_MINUTES

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=JWT_ACCESS_MINUTES),
    "REFRESH_TOKEN_LIFETIME": timedelta(minutes=JWT_REFRESH_MINUTES),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
}

CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# In development the machine's LAN IP changes (DHCP) and the trainer app /
# Expo web client connect from that IP. Accept any host/origin while DEBUG so
# the app stays online without editing .env every day. Production is locked
# down by the explicit lists above.
if DEBUG:
    ALLOWED_HOSTS = ["*"]
    CORS_ALLOW_ALL_ORIGINS = True

# Security headers (HTTPS enforced at proxy; HSTS enabled when not in DEBUG)
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Photo-attendance vision provider: AWS Rekognition face matching against
# consented student reference photos. This is the only supported provider.
ATTENDANCE_VISION_PROVIDER = os.environ.get("ATTENDANCE_VISION_PROVIDER", "faceauth")

# Face-group (student) matching thresholds used by faceauth's decision engine.
# A face is auto-marked Present only at/above FACE_GROUP_MATCH_THRESHOLD; an
# enrolled athlete whose face is not detected is auto-marked Absent.
FACE_GROUP_MATCH_THRESHOLD = float(os.environ.get("FACE_GROUP_MATCH_THRESHOLD", "90"))
FACE_GROUP_REVIEW_THRESHOLD = float(os.environ.get("FACE_GROUP_REVIEW_THRESHOLD", "80"))
FACE_GROUP_AMBIGUITY_MARGIN = float(os.environ.get("FACE_GROUP_AMBIGUITY_MARGIN", "3"))

# Face matching engine (AWS Rekognition). boto3 reads the AWS credentials from
# the environment directly; these settings drive collection naming/thresholds.
FACE_VERIFICATION_PROVIDER = os.environ.get("FACE_VERIFICATION_PROVIDER", "aws")
AWS_REGION = os.environ.get("AWS_REGION", "")
AWS_LIVENESS_REGION = os.environ.get("AWS_LIVENESS_REGION", "") or AWS_REGION
AWS_REKOGNITION_COLLECTION_PREFIX = os.environ.get("AWS_REKOGNITION_COLLECTION_PREFIX", "")
FACE_MATCH_THRESHOLD = float(os.environ.get("FACE_MATCH_THRESHOLD", "90"))
FACE_LIVENESS_MIN_CONFIDENCE = float(os.environ.get("FACE_LIVENESS_MIN_CONFIDENCE", "90"))

# File upload validation
MAX_UPLOAD_SIZE_MB = 10
ALLOWED_UPLOAD_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "csv", "xlsx"}

# Integrations (all credential-free defaults; providers read env at runtime)
PAYMENT_GATEWAY_PROVIDER = os.environ.get("PAYMENT_GATEWAY_PROVIDER", "mock")
WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "mock")

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
# Scheduled jobs (run only when a Celery beat + worker are deployed). The same
# sweep is available via `manage.py process_trainer_attendance` for cron.
CELERY_BEAT_SCHEDULE = {
    "process-trainer-attendance": {
        "task": "apps.coaches.tasks.process_trainer_attendance",
        "schedule": 300.0,  # every 5 minutes
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}


# =====================================================
# Face verification (faceauth app).
# FACE_VERIFICATION_PROVIDER: "none" (default, fail-closed)
# or "aws" (Rekognition + configured mobile liveness SDK).
# Never treated as verified unless a real provider confirms.
# =====================================================
 
FACE_VERIFICATION_PROVIDER = os.getenv("FACE_VERIFICATION_PROVIDER", "none")
FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "90"))
FACE_REKOGNITION_COLLECTION_ID = os.getenv(
    "FACE_REKOGNITION_COLLECTION_ID", "cliccot-faces"
)
AWS_REKOGNITION_COLLECTION_PREFIX = os.getenv(
    "AWS_REKOGNITION_COLLECTION_PREFIX", ""
)
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
 
# --- Live-only face check (AWS Rekognition Face Liveness) ---------
# When True (default) the old still-photo endpoints (/api/face/verify/,
# /verify-session/, multipart /enroll/) return 410 and attendance can only be
# verified through /api/face/liveness/start/ + /complete/.
FACE_REQUIRE_LIVENESS = os.getenv("FACE_REQUIRE_LIVENESS", "True").lower() in ("1", "true", "yes")
# Region where Face Liveness is available (defaults to AWS_REGION).
AWS_LIVENESS_REGION = os.getenv("AWS_LIVENESS_REGION", "") or AWS_REGION
# AWS liveness confidence (0-100) required to pass.
FACE_LIVENESS_MIN_CONFIDENCE = float(os.getenv("FACE_LIVENESS_MIN_CONFIDENCE", "90"))
# IAM role the backend assumes to give the phone 15-min credentials that can
# ONLY call rekognition:StartFaceLivenessSession. Leave empty to use Cognito.
AWS_LIVENESS_ROLE_ARN = os.getenv("AWS_LIVENESS_ROLE_ARN", "")
# Only needed if the reference image is not returned inline.
AWS_LIVENESS_S3_BUCKET = os.getenv("AWS_LIVENESS_S3_BUCKET", "")
FACE_LIVENESS_AUDIT_IMAGES = int(os.getenv("FACE_LIVENESS_AUDIT_IMAGES", "0"))
FACE_MIN_TEMPLATES = int(os.getenv("FACE_MIN_TEMPLATES", "1"))
FACE_MAX_STARTS_PER_10_MIN = int(os.getenv("FACE_MAX_STARTS_PER_10_MIN", "6"))
 
 
