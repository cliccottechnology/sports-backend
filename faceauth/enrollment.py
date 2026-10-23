"""Index the academy's consented student reference photos into the face
collection, and build the lookup tables the decision engine needs.

Only reference photos with recorded guardian consent are indexed; a student
without a consented photo simply has no face enrolled and will always be
surfaced to the coach as "needs review" (never silently absent). The provider
returns an opaque `face_id` per enrolled photo which we keep in
`StudentFaceTemplate` so one attendance photo can identify many athletes.
"""
import logging

from apps.students.models import Student, StudentReferencePhoto

from .models import StudentFaceTemplate
from .providers import get_provider

logger = logging.getLogger(__name__)


def _as_str(value):
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore")
    return str(value)


def _read(ff):
    """Read bytes from a FieldFile / uploaded file without leaking the handle."""
    try:
        ff.seek(0)
    except Exception:
        pass
    data = ff.read()
    if isinstance(data, str):
        data = data.encode("latin-1")
    return data


def index_student_faces(student_ids, school_id=None, provider=None):
    """Bring `StudentFaceTemplate` in sync with consented reference photos.

    Adds a template for every active, consented photo not yet indexed and
    removes (locally + in the provider collection) templates whose source photo
    is gone, inactive, or no longer consented. Best-effort per photo — one bad
    image never blocks the whole attendance run. Returns the number added.
    """
    ids = sorted({int(i) for i in student_ids})
    if not ids:
        return 0
    provider = provider or get_provider()
    wanted = StudentReferencePhoto.objects.filter(
        student_id__in=ids, is_active=True, consent_given=True,
    ).select_related("student")
    wanted_ids = set(wanted.values_list("pk", flat=True))
    existing = {
        t.source_photo_id: t
        for t in StudentFaceTemplate.objects.filter(
            student_id__in=ids, provider=provider.name,
            source_photo_id__isnull=False,
        )
    }
    added = 0
    for photo in wanted:
        if photo.pk in existing:
            continue
        try:
            face_ids = provider.enroll(
                [_read(photo.image)], company_id=school_id,
                # Unique per source photo. Rekognition REPLACES a face and
                # returns the SAME FaceId when an ExternalImageId is reused, so a
                # student-only id would make every extra reference photo of that
                # student collide on the face_id unique constraint.
                employee_code=f"student-{photo.student_id}-photo-{photo.pk}",
                scope="students",
            )
        except Exception as exc:  # noqa: BLE001 - one photo must not stop the run
            logger.warning("Could not index reference photo %s: %s", photo.pk, exc)
            continue
        if not face_ids:
            continue
        for fid in face_ids:
            # update_or_create on the unique face_id: if a provider ever returns
            # an already-known face, we re-point it instead of 500-ing the whole
            # photo analysis on an IntegrityError.
            StudentFaceTemplate.objects.update_or_create(
                face_id=_as_str(fid),
                defaults=dict(
                    school_id=photo.student.school_id or school_id,
                    student_id=photo.student_id,
                    source_photo=photo,
                    provider=provider.name,
                    consent_given=True,
                ),
            )
        added += 1

    stale = [
        t for t in StudentFaceTemplate.objects.filter(
            student_id__in=ids, provider=provider.name,
        )
        if t.source_photo_id not in wanted_ids
    ]
    if stale:
        try:
            provider.delete_faces(
                [t.face_id for t in stale], company_id=school_id, scope="students",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Provider face purge failed: %s", exc)
        StudentFaceTemplate.objects.filter(
            pk__in=[t.pk for t in stale],
        ).delete()
    return added


def build_face_context(student_ids, school_id=None, provider=None):
    """Return roster + face lookups for `group_service.decide()`.

    `owner_by_face` maps a provider face id to the student key ("<pk>"), so a
    match can never be credited to a student outside this roster.
    """
    ids = sorted({int(i) for i in student_ids})
    provider = provider or get_provider()
    students = {s.pk: s for s in Student.objects.filter(pk__in=ids)}
    templates = StudentFaceTemplate.objects.filter(
        student_id__in=ids, provider=provider.name,
    )
    owner_by_face = {t.face_id: str(t.student_id) for t in templates}
    enrolled_keys = {str(t.student_id) for t in templates}
    roster = []
    for i in ids:
        s = students.get(i)
        name = f"{s.first_name} {s.last_name}".strip() if s else str(i)
        roster.append({"student_key": str(i), "name": name})
    return {
        "roster": roster,
        "owner_by_face": owner_by_face,
        "enrolled_keys": enrolled_keys,
        # Only consented photos are ever indexed, so enrolled == consented.
        "consent_keys": set(enrolled_keys),
        "provider": provider,
    }
