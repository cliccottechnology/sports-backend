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
