"""Report the active photo-attendance vision provider and (optionally) probe AWS.

Usage:
    python manage.py face_provider_status
    python manage.py face_provider_status --check-aws
"""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Show the active photo-attendance face provider and optionally probe AWS Rekognition."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-aws", action="store_true",
            help="Make a live Rekognition call to verify credentials/connectivity.",
        )

    def handle(self, *args, **options):
        from apps.attendance.vision import get_provider

        vision = getattr(settings, "ATTENDANCE_VISION_PROVIDER", "faceauth")
        face = getattr(settings, "FACE_VERIFICATION_PROVIDER", "none")
        self.stdout.write(f"ATTENDANCE_VISION_PROVIDER = {vision}")
        self.stdout.write(f"FACE_VERIFICATION_PROVIDER = {face}")
        self.stdout.write(f"AWS_REGION                = {getattr(settings, 'AWS_REGION', '') or '(unset)'}")

        try:
            name = get_provider().name
            style = self.style.SUCCESS if vision == name else self.style.WARNING
            self.stdout.write(style(f"Active vision provider   = {name}"))
        except Exception as exc:  # noqa: BLE001 - surface the misconfiguration
            self.stdout.write(self.style.ERROR(f"Could not resolve provider: {exc}"))
            return

        if not options["check_aws"]:
            return

        if str(face).lower() != "aws":
            self.stdout.write(self.style.WARNING("--check-aws skipped: face engine is not 'aws'."))
            return

        from faceauth.providers import get_provider as get_face_provider

        try:
            collections = get_face_provider().ping()
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f"AWS Rekognition check FAILED: {exc}"))
            return
        self.stdout.write(self.style.SUCCESS(f"AWS Rekognition OK. Collections: {collections}"))
