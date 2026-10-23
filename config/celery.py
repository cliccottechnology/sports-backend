"""Celery application (optional).

Only used when the deployment runs a Celery worker + beat with Redis. The
attendance sweep is also available as a management command for cron / Windows
Task Scheduler, so the feature works with or without Celery.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("jetz")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
