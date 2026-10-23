"""Startup / system checks for the photo-attendance face provider.

These are configuration-level checks ONLY: they never call AWS, so
``manage.py check`` and every server start stay fast and never crash when the
cloud is temporarily unreachable. For a live connectivity probe run
``python manage.py face_provider_status --check-aws``.

The point is to make the active provider impossible to get silently wrong:
attendance must be pointed at ``faceauth`` with a real face engine configured,
otherwise Django reports it instead of serving a fabricated result.
"""
from django.core.checks import Error, Warning, register

_KNOWN_VISION = {"faceauth"}
_REAL_FACE_ENGINES = {"aws", "mock", "sandbox", "test"}


@register()
def attendance_vision_provider_check(app_configs, **kwargs):
    from django.conf import settings

    issues = []
    vision = str(getattr(settings, "ATTENDANCE_VISION_PROVIDER", "faceauth") or "faceauth")
    if vision not in _KNOWN_VISION:
        issues.append(Error(
            f"ATTENDANCE_VISION_PROVIDER={vision!r} is not a known provider "
            f"(expected one of: {', '.join(sorted(_KNOWN_VISION))}).",
            id="faceauth.E001",
        ))
        return issues

    face = str(getattr(settings, "FACE_VERIFICATION_PROVIDER", "none") or "none").lower()
    if face not in _REAL_FACE_ENGINES:
        issues.append(Error(
            "ATTENDANCE_VISION_PROVIDER=faceauth requires a real face engine. "
            f"Set FACE_VERIFICATION_PROVIDER=aws (or mock in tests); got {face!r}. "
            "Real face recognition is not configured.",
            id="faceauth.E002",
        ))
        return issues

    if face in ("mock", "sandbox", "test"):
        is_demo_or_test = (
            getattr(settings, "DEBUG", False)
            or getattr(settings, "ALLOW_MOCK_FACE_AUTH", False)
            or getattr(settings, "TESTING", False)
        )
        if not is_demo_or_test:
            issues.append(Error(
                "Mock face verification provider ('mock') is not permitted in production. "
                "Set FACE_VERIFICATION_PROVIDER=aws with valid AWS credentials.",
                id="faceauth.E004",
            ))

    if face == "aws":
        import os
        try:
            import boto3  # noqa: F401
        except ImportError:
            issues.append(Error(
                "FACE_VERIFICATION_PROVIDER=aws requires boto3. Run: pip install boto3",
                id="faceauth.E003",
            ))
        if not str(getattr(settings, "AWS_REGION", "") or "").strip():
            issues.append(Warning(
                "AWS_REGION is empty; Rekognition will fall back to ap-south-1.",
                id="faceauth.W001",
            ))
        if not (os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")):
            issues.append(Warning(
                "No AWS credentials found in the environment; face matching will "
                "raise a clear config error until they are provided.",
                id="faceauth.W002",
            ))
    return issues
