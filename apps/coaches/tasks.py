"""Celery tasks for trainer attendance automation.

The task is a thin wrapper so it can also be triggered from Celery beat. The
real logic lives in attendance_automation.process_trainer_attendance and is
idempotent, so concurrent or repeated runs are safe.
"""
try:
    from celery import shared_task
except Exception:  # Celery optional
    def shared_task(*args, **kwargs):
        def deco(fn):
            return fn
        return deco


@shared_task(name="apps.coaches.tasks.process_trainer_attendance")
def process_trainer_attendance():
    from .attendance_automation import process_trainer_attendance as _run
    return _run()
