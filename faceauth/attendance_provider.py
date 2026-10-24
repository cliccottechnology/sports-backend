"""Face provider that plugs the faceauth engine into the existing
photo-attendance API contract.

`apps.attendance.vision.get_provider()` returns this class when
``ATTENDANCE_VISION_PROVIDER=faceauth``. It keeps the existing endpoint,
review screen and ``bulk_mark`` confirmation flow intact: it only turns a
group photo into per-student suggestions (``verified`` / ``not_detected`` /
``needs_review``) and never writes attendance itself.
"""
import logging

from django.conf import settings

from apps.attendance.vision import AnalysisResult, BaseVisionProvider, StudentSuggestion

from .enrollment import build_face_context, index_student_faces
from .group_service import decide
from .providers import ProviderNotConfigured, get_provider

logger = logging.getLogger(__name__)

_SUGGESTION_BY_STATUS = {"PRESENT": "verified", "ABSENT": "not_detected"}


def _read_bytes(image_file):
    if isinstance(image_file, (bytes, bytearray)):
        return bytes(image_file)
    if hasattr(image_file, "read"):
        try:
            image_file.seek(0)
        except Exception:
            pass
        data = image_file.read()
        if isinstance(data, str):
            data = data.encode("latin-1")
        return data
    with open(image_file, "rb") as fh:
        return fh.read()


def _school_id_for(session, roster_ids):
    school_id = None
    batch = getattr(session, "batch", None) if session is not None else None
    if batch is not None:
        school_id = getattr(batch, "school_id", None)
    if school_id:
        return school_id
    from apps.students.models import Student
    found = {
        s for s in Student.objects.filter(pk__in=roster_ids)
        .values_list("school_id", flat=True) if s
    }
    return found.pop() if len(found) == 1 else None


class FaceVisionProvider(BaseVisionProvider):
    """AWS Rekognition group identification, mapped to the attendance contract."""

    name = "faceauth"

    def analyze(self, image_file, roster_student_ids, session=None):
        raw = _read_bytes(image_file)
        school_id = _school_id_for(session, roster_student_ids)
        provider = get_provider()
        if provider.name in ("none", "base"):
            raise ValueError(
                "Face recognition is not configured. Set FACE_VERIFICATION_PROVIDER=aws."
            )
        review_threshold = float(getattr(settings, "FACE_GROUP_REVIEW_THRESHOLD", 80.0))
        try:
            index_student_faces(roster_student_ids, school_id, provider)
            context = build_face_context(roster_student_ids, school_id, provider)
            observations = provider.identify_faces(
                raw, company_id=school_id, scope="students",
                min_similarity=review_threshold,
            )
        except ProviderNotConfigured:
            raise
        except Exception as exc:  # noqa: BLE001 - surface a clear, secret-free message
            logger.exception("Face analysis failed")
            raise ValueError(
                f"Face recognition could not analyse this photo: {exc}"
            ) from exc

        decision = decide(
            [observations], context["roster"], context["owner_by_face"],
            context["enrolled_keys"], context["consent_keys"],
        )



        suggestions = []
        names = {}
        not_enrolled, needs_review = [], []
        for key, row in decision["students"].items():
            names[key] = row["name"]
            suggestion = _SUGGESTION_BY_STATUS.get(row["status"], "needs_review")
            suggestions.append(StudentSuggestion(
                student_id=int(key),
                suggestion=suggestion,
                confidence=row.get("similarity"),
                reason="Auto-matched from the group photo."
                if suggestion == "verified"
                else "Confirm against the group photo."
                if suggestion == "needs_review"
                else "No enrolled face found in this photo.",
            ))
            if row["status"] == "NOT_ENROLLED":
                not_enrolled.append(row["name"])
            elif row["status"] == "REVIEW":
                needs_review.append(row["name"])

        unknown = len(decision["unknown_faces"])
        failed = sum(1 for o in observations if o.get("search_error"))
        notes = []
        if failed:
            notes.append(
                f"{failed} face(s) could not be checked because the recognition "
                "service rate-limited the request. Run the photo analysis again "
                "before saving — do not treat this result as complete."
            )
        if unknown:
            notes.append(
                f"{unknown} face(s) in the photo did not match any athlete on this roster."
            )
        if not_enrolled:
            notes.append(
                "No consented reference photo on file (cannot be auto-matched): "
                + ", ".join(not_enrolled[:10])
            )
        if needs_review:
            notes.append("Please confirm: " + ", ".join(needs_review[:10]))

        return AnalysisResult(
            provider=self.name,
            persons_detected=decision["faces_detected"],
            suggestions=suggestions,
            unrecognized_count=unknown,
            notes=notes,
        )
