# Import the Celery app when Celery is installed so shared_task decorators bind
# to it. Guarded so Django still boots in environments without Celery/Redis
# (the attendance sweep then runs via the management command).
try:
    from .celery import app as celery_app

    __all__ = ("celery_app",)
except Exception:  # pragma: no cover - Celery optional
    celery_app = None
