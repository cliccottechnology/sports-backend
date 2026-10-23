from django.apps import AppConfig


class SessionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sessions"
    # Must differ from django.contrib.sessions' label ("sessions").
    label = "training_sessions"
