"""Tests for the faceauth photo-attendance provider and the session-roster
self-heal that removes the "Session has no roster to analyze against." error."""
from datetime import date, time
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient


def _jpeg(color="navy", size=(800, 600)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    buf.seek(0)
    return SimpleUploadedFile("group.jpg", buf.read(), content_type="image/jpeg")


@pytest.fixture
def academy_setup():
    from apps.academy.models import School, Sport, TrainingLocation
    from apps.accounts.models import Permission, Role, User
    from apps.batches.models import Batch, BatchStudent
    from apps.coaches.models import Coach
    from apps.sessions.models import Session
    from apps.students.models import Student

    school = School.objects.create(name="FA-Academy", code="FAAC")
    sport = Sport.objects.create(name="FA-Football", code="FAFB")
    coach = Coach.objects.create(trainer_id="FA-T1", first_name="Fay", phone="7",
                                 weekly_off_days=[])
    loc = TrainingLocation.objects.create(name="FA-Loc", school=school)
    batch = Batch.objects.create(name="FA-B", school=school, sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    st1 = Student.objects.create(student_id="FA-S1", first_name="Asha",
                                 enrollment_date=date.today(), sport=sport, school=school)
    st2 = Student.objects.create(student_id="FA-S2", first_name="Dev",
                                 enrollment_date=date.today(), sport=sport, school=school)
    # Students are on the BATCH roster but the session is created with none.
    BatchStudent.objects.create(batch=batch, student=st1)
    BatchStudent.objects.create(batch=batch, student=st2)
    sess = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                  start_time=time(7, 0), end_time=time(8, 0), location=loc)
    role = Role.objects.create(name="faceauth_coach")
    for code in ("attendance.view", "attendance.mark"):
        perm, _ = Permission.objects.get_or_create(code=code, defaults={"name": code})
        role.permissions.add(perm)
    user = User.objects.create(username="fa-coach", role=role, is_active=True,
                               coach_profile=coach)
    user.set_password("pass12345")
    user.save()
    return {"school": school, "batch": batch, "session": sess,
            "st1": st1, "st2": st2, "user": user}


@pytest.mark.django_db
def test_roster_endpoint_backfills_empty_session_from_batch(academy_setup):
    from apps.sessions.models import SessionStudent
    sess = academy_setup["session"]
    assert SessionStudent.objects.filter(session=sess).count() == 0
    client = APIClient()
    client.force_authenticate(academy_setup["user"])
    r = client.get(f"/api/attendance/roster/?session={sess.pk}")
    assert r.status_code == 200, r.content
    assert len(r.data["roster"]) == 2
    assert SessionStudent.objects.filter(session=sess).count() == 2
    # Idempotent: a second call does not duplicate rows.
    client.get(f"/api/attendance/roster/?session={sess.pk}")
    assert SessionStudent.objects.filter(session=sess).count() == 2


@pytest.mark.django_db
def test_photo_analyze_faceauth_provider_matches_enrolled_student(academy_setup, settings):
    from apps.attendance.models import AttendanceRecord
    from apps.students.models import StudentReferencePhoto
    from faceauth.providers import MockFaceProvider

    MockFaceProvider.registry.clear()
    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "mock"

    st1 = academy_setup["st1"]
    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    StudentReferencePhoto.objects.create(
        student=st1, consent_given=True, consented_by="Parent",
        image=SimpleUploadedFile("ref.jpg", buf.read(), content_type="image/jpeg"),
    )

    client = APIClient()
    client.force_authenticate(academy_setup["user"])
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": academy_setup["session"].pk, "photo": _jpeg()},
                    format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["provider"] == "faceauth"
    by_id = {s["student"]: s for s in r.data["suggestions"]}
    assert by_id[st1.pk]["suggestion"] == "verified"
    assert by_id[academy_setup["st2"].pk]["suggestion"] == "needs_review"
    # Analysis never writes attendance: the coach still confirms via bulk_mark.
    assert AttendanceRecord.objects.filter(session=academy_setup["session"]).count() == 0

    # A repeat analysis must not 500 on re-indexing the same reference photos.
    r2 = client.post("/api/attendance/photo_analyze/",
                     {"session": academy_setup["session"].pk, "photo": _jpeg()},
                     format="multipart")
    assert r2.status_code == 200, r2.content
    assert r2.data["provider"] == "faceauth"


@pytest.mark.django_db
def test_photo_analyze_auto_submit_marks_present_and_absent_with_unenrolled_students(academy_setup, settings):
    from apps.attendance.models import AttendanceRecord
    from apps.batches.models import BatchStudent
    from apps.students.models import Student, StudentReferencePhoto
    from faceauth.models import StudentFaceTemplate
    from faceauth.providers import MockFaceProvider

    MockFaceProvider.registry.clear()
    MockFaceProvider.scripted = None
    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "mock"

    st1 = academy_setup["st1"]
    st2 = academy_setup["st2"]
    # Add a 3rd student without reference photo
    st3 = Student.objects.create(student_id="FA-S3", first_name="Unenrolled",
                                 enrollment_date=date.today(), sport=academy_setup["batch"].sport,
                                 school=academy_setup["school"])
    BatchStudent.objects.create(batch=academy_setup["batch"], student=st3)

    # Enroll st1 and st2 with reference photos
    for st in (st1, st2):
        buf = BytesIO()
        Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
        buf.seek(0)
        StudentReferencePhoto.objects.create(
            student=st, consent_given=True, consented_by="Parent",
            image=SimpleUploadedFile(f"ref_{st.pk}.jpg", buf.read(), content_type="image/jpeg"),
        )

    # Ensure templates are indexed so we can get st1's face_id for scripted observation
    from faceauth.enrollment import index_student_faces
    from faceauth.providers import get_provider
    provider = get_provider()
    index_student_faces([st1.pk, st2.pk], academy_setup["school"].pk, provider)
    st1_tmpl = StudentFaceTemplate.objects.filter(student=st1).first()
    assert st1_tmpl is not None

    # Script MockFaceProvider to detect ONLY st1 in the photo
    MockFaceProvider.scripted = [{
        "box": {"left": 0.1, "top": 0.1, "width": 0.1, "height": 0.1},
        "detection_confidence": 99.0,
        "quality": {"brightness": 70.0, "sharpness": 70.0},
        "flags": [],
        "matches": [{"face_id": st1_tmpl.face_id, "similarity": 98.0}],
    }]

    client = APIClient()
    client.force_authenticate(academy_setup["user"])
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": academy_setup["session"].pk, "photo": _jpeg(), "auto_submit": "true"},
                    format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["submitted"] is True

    # st1 is enrolled and detected -> present
    rec1 = AttendanceRecord.objects.get(session=academy_setup["session"], student=st1)
    assert rec1.status == "present"

    # st2 is enrolled but NOT detected -> automatically marked absent
    rec2 = AttendanceRecord.objects.get(session=academy_setup["session"], student=st2)
    assert rec2.status == "absent"

    # st3 is NOT enrolled (no photo) -> not auto-marked absent (leaves for review)
    assert AttendanceRecord.objects.filter(session=academy_setup["session"], student=st3).count() == 0

    MockFaceProvider.scripted = None


@pytest.mark.django_db
def test_photo_analyze_empty_photo_marks_enrolled_absent(academy_setup, settings):
    from apps.attendance.models import AttendanceRecord
    from apps.students.models import StudentReferencePhoto
    from faceauth.providers import MockFaceProvider

    MockFaceProvider.registry.clear()
    MockFaceProvider.scripted = []  # No faces detected (empty frame)
    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "mock"

    st1 = academy_setup["st1"]
    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    StudentReferencePhoto.objects.create(
        student=st1, consent_given=True, consented_by="Parent",
        image=SimpleUploadedFile("ref.jpg", buf.read(), content_type="image/jpeg"),
    )

    client = APIClient()
    client.force_authenticate(academy_setup["user"])

    # Without auto_submit: suggestions show enrolled athlete as not_detected / absent
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": academy_setup["session"].pk, "photo": _jpeg()},
                    format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["status"] == "SUCCESS"
    by_id = {s["student"]: s for s in r.data["suggestions"]}
    assert by_id[st1.pk]["suggestion"] == "not_detected"
    assert by_id[st1.pk]["status"] == "absent"

    # With auto_submit: enrolled student is saved in DB as absent
    r_auto = client.post("/api/attendance/photo_analyze/",
                         {"session": academy_setup["session"].pk, "photo": _jpeg(), "auto_submit": "true"},
                         format="multipart")
    assert r_auto.status_code == 200, r_auto.content
    assert r_auto.data["submitted"] is True
    rec = AttendanceRecord.objects.get(session=academy_setup["session"], student=st1)
    assert rec.status == "absent"

    MockFaceProvider.scripted = None


