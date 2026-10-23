"""Regression tests for reference-photo indexing.

Reproduces the field bug where a second reference photo of the same student
made AWS return the same FaceId (ExternalImageId reuse), which blew up the
whole photo analysis with a 500 on the face_id unique constraint.
"""
from datetime import date
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image


class _StubProvider:
    """Returns whatever face ids it is told, ignoring the image."""

    name = "stub"

    def __init__(self, face_ids):
        self._face_ids = face_ids
        self.codes = []

    def enroll(self, image_bytes_list, company_id=None, employee_code="", scope=""):
        self.codes.append(employee_code)
        return list(self._face_ids)

    def delete_faces(self, face_ids, company_id=None, scope=""):
        return 0


class _UniqueIdProvider(_StubProvider):
    def __init__(self):
        super().__init__(None)

    def enroll(self, image_bytes_list, company_id=None, employee_code="", scope=""):
        self.codes.append(employee_code)
        return [f"face-{len(self.codes)}".encode()]


def _ref_photo(student, name="ref.jpg"):
    from io import BytesIO

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    from apps.students.models import StudentReferencePhoto

    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    return StudentReferencePhoto.objects.create(
        student=student, consent_given=True, consented_by="Parent",
        image=SimpleUploadedFile(name, buf.read(), content_type="image/jpeg"),
    )


@pytest.fixture
def student(db):
    from datetime import date

    from apps.academy.models import School, Sport
    from apps.students.models import Student

    school = School.objects.create(name="ENR-Academy", code="ENRA")
    sport = Sport.objects.create(name="ENR-Football", code="ENRF")
    return Student.objects.create(
        student_id="ENR-S1", first_name="Ira", enrollment_date=date.today(),
        sport=sport, school=school,
    )


@pytest.mark.django_db
def test_enroll_uses_unique_external_id_per_photo(student):
    from faceauth.enrollment import index_student_faces

    _ref_photo(student, "one.jpg")
    _ref_photo(student, "two.jpg")
    spy = _UniqueIdProvider()
    index_student_faces([student.pk], student.school_id, spy)
    assert len(spy.codes) == 2
    assert spy.codes[0] != spy.codes[1]


@pytest.mark.django_db
def test_shared_face_id_across_photos_does_not_crash(student):
    from faceauth.enrollment import index_student_faces
    from faceauth.models import StudentFaceTemplate

    _ref_photo(student, "one.jpg")
    _ref_photo(student, "two.jpg")
    # Rekognition returns the SAME FaceId when the ExternalImageId is reused.
    stub = _StubProvider([b"same-face-id"])
    added = index_student_faces([student.pk], student.school_id, stub)
    assert added == 2
    # The regression: no IntegrityError; the shared face is stored exactly once.
    assert StudentFaceTemplate.objects.count() == 1
    assert StudentFaceTemplate.objects.get().face_id == "same-face-id"


@pytest.mark.django_db
def test_indexing_twice_is_idempotent(student):
    from faceauth.enrollment import index_student_faces
    from faceauth.models import StudentFaceTemplate

    _ref_photo(student)
    stub = _UniqueIdProvider()
    index_student_faces([student.pk], student.school_id, stub)
    index_student_faces([student.pk], student.school_id, stub)
    assert StudentFaceTemplate.objects.filter(student=student).count() == 1
