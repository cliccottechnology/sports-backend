"""Pluggable vision provider for AI-assisted photo attendance.

Privacy design:
- Only consented reference photos are indexed as biometric templates.
- The provider matches faces in the group photo against those enrolled
  templates (AWS Rekognition via the ``faceauth`` provider).
- A face that matches an enrolled roster athlete -> ``verified`` (Present).
- An enrolled athlete whose face is NOT found in the photo -> ``not_detected``
  (Absent).
- An athlete with no consented reference photo cannot be compared -> they are
  never marked and are only reported as a not-enrolled count.
"""
from dataclasses import dataclass, field

from django.conf import settings

SUGGESTION_CHOICES = ("verified", "not_detected", "needs_review")


@dataclass
class StudentSuggestion:
    student_id: int  # PK of students.Student
    suggestion: str  # one of SUGGESTION_CHOICES
    confidence: float | None
    reason: str


@dataclass
class AnalysisResult:
    provider: str
    persons_detected: int | None
    suggestions: list = field(default_factory=list)
    unrecognized_count: int = 0
    notes: list = field(default_factory=list)
    status: str = "SUCCESS"
    message: str = ""
    status: str = "SUCCESS"
    message: str = ""


class BaseVisionProvider:
    name = "base"

    def analyze(self, image_file, roster_student_ids: list[int],
                session=None) -> AnalysisResult:
        """Return per-student suggestions for a session group photo.

        ``session`` is optional context (school/roster scoping) supplied by the
        caller; providers that don't need it may ignore it.
        """
        raise NotImplementedError


def dhash_hex(image, size: int = 8) -> str:
    """Perceptual difference-hash (Pillow only, no numpy). Returns 16 hex chars."""
    gray = image.convert("L").resize((size + 1, size))
    px = list(gray.getdata())
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if px[y * (size + 1) + x] > px[y * (size + 1) + x + 1] else 0)
    return f"{bits:016x}"


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def get_provider() -> BaseVisionProvider:
    """Return the face-matching provider.

    AWS Rekognition (``faceauth``) is the only supported provider for AI
    attendance. Any other value fails closed with a clear error rather than
    silently returning a fabricated result.
    """
    name = str(getattr(settings, "ATTENDANCE_VISION_PROVIDER", "faceauth") or "faceauth")
    if name == "faceauth":
        # Imported lazily so the AWS deps (boto3/numpy) are only required for
        # this provider.
        from faceauth.attendance_provider import FaceVisionProvider
        return FaceVisionProvider()
    raise ValueError(
        f"Unknown ATTENDANCE_VISION_PROVIDER: {name!r}. "
        "Set ATTENDANCE_VISION_PROVIDER=faceauth."
    )
