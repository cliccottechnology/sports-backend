from django.apps import AppConfig

_logged = False


class FaceauthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "faceauth"
    verbose_name = "Face Verification Attendance"

    def ready(self):
        # Registers the provider configuration system checks (see checks.py).
        from . import checks  # noqa: F401

        global _logged
        import os
        import sys

        if _logged:
            return
        if 'runserver' in sys.argv and os.environ.get('RUN_MAIN') != 'true':
            # Let the reloader child handle printing to avoid duplicate on startup
            _logged = True
            return

        _logged = True

        from django.conf import settings

        msg = "Photo-attendance vision provider=%s (face engine=%s)" % (
            getattr(settings, "ATTENDANCE_VISION_PROVIDER", "faceauth"),
            getattr(settings, "FACE_VERIFICATION_PROVIDER", "none"),
        )
        print(msg, flush=True)
