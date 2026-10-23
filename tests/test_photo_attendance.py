"""Tests for AI-assisted photo attendance endpoint (faceauth provider)."""
from datetime import date, time

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from io import BytesIO
from rest_framework.test import APIClient


def _photo(color="navy", size=(800, 600)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    buf.seek(0)
    return SimpleUploadedFile("group.jpg", buf.read(), content_type="image/jpeg")


@pytest.fixture
def coach_setup():
    from apps.academy.models import Sport, TrainingLocation
    from apps.accounts.models import Permission, Role, User
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student
    sport = Sport.objects.create(name="PA-Football", code="PAFB")
    coach = Coach.objects.create(trainer_id="PA-T1", first_name="Pam", phone="9", weekly_off_days=[])
    loc = TrainingLocation.objects.create(name="PA-Loc")
    batch = Batch.objects.create(name="PA-B", sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    st1 = Student.objects.create(student_id="PA-S1", first_name="Asha", enrollment_date=date.today(), sport=sport)
    st2 = Student.objects.create(student_id="PA-S2", first_name="Dev", enrollment_date=date.today(), sport=sport)
    sess = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                  start_time=time(7, 0), end_time=time(8, 0), location=loc)
    SessionStudent.objects.create(session=sess, student=st1)
    SessionStudent.objects.create(session=sess, student=st2)
    perm, _ = Permission.objects.get_or_create(code="attendance.mark",
                                               defaults={"name": "mark", "module": "attendance"})
    role, _ = Role.objects.get_or_create(name="coach")
    role.permissions.add(perm)
    user = User(username="photo_coach", role=role, is_active=True, coach_profile=coach)
    user.set_password("pass12345")
    user.save()
    return {"user": user, "session": sess}


@pytest.mark.django_db
def test_photo_analyze_reports_needs_review_without_references(coach_setup):
    client = APIClient()
    client.force_authenticate(coach_setup["user"])
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": coach_setup["session"].pk, "photo": _photo()}, format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["provider"] == "faceauth"
    assert len(r.data["suggestions"]) == 2
    # No enrolled reference photos: nobody can be matched, so nobody is marked.
    assert {s["suggestion"] for s in r.data["suggestions"]} == {"needs_review"}
    assert all(s["has_reference"] is False for s in r.data["suggestions"])
    assert r.data["photo_id"] is not None
    # No attendance records written by analysis.
    from apps.attendance.models import AttendanceRecord
    assert AttendanceRecord.objects.filter(session=coach_setup["session"]).count() == 0


@pytest.mark.django_db
def test_photo_analyze_rejects_non_image(coach_setup):
    client = APIClient()
    client.force_authenticate(coach_setup["user"])
    bad = SimpleUploadedFile("x.txt", b"not an image", content_type="text/plain")
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": coach_setup["session"].pk, "photo": bad}, format="multipart")
    assert r.status_code == 400


@pytest.mark.django_db
def test_photo_analyze_forbids_other_coach_session(coach_setup):
    from apps.coaches.models import Coach
    from apps.accounts.models import User
    other = Coach.objects.create(trainer_id="PA-T9", first_name="Zed", phone="8", weekly_off_days=[])
    intruder = User(username="intruder", role=coach_setup["user"].role,
                    is_active=True, coach_profile=other)
    intruder.set_password("pass12345")
    intruder.save()
    client = APIClient()
    client.force_authenticate(intruder)
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": coach_setup["session"].pk, "photo": _photo()}, format="multipart")
    assert r.status_code == 403


@pytest.mark.django_db
def test_photo_analyze_flags_duplicate_submission(coach_setup):
    client = APIClient()
    client.force_authenticate(coach_setup["user"])
    url = "/api/attendance/photo_analyze/"
    sid = coach_setup["session"].pk
    r1 = client.post(url, {"session": sid, "photo": _photo()}, format="multipart")
    assert r1.status_code == 200, r1.content
    assert r1.data["possible_duplicate"] is None
    r2 = client.post(url, {"session": sid, "photo": _photo()}, format="multipart")
    assert r2.status_code == 200, r2.content
    assert r2.data["possible_duplicate"] == r1.data["photo_id"]


@pytest.mark.django_db
def test_photo_analyze_reports_reference_enrollment(coach_setup):
    from apps.students.models import Student, StudentReferencePhoto
    st = Student.objects.get(student_id="PA-S1")
    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    StudentReferencePhoto.objects.create(
        student=st, consent_given=True, consented_by="Parent",
        image=SimpleUploadedFile("ref.jpg", buf.read(), content_type="image/jpeg"),
    )
    client = APIClient()
    client.force_authenticate(coach_setup["user"])
    r = client.post("/api/attendance/photo_analyze/",
                    {"session": coach_setup["session"].pk, "photo": _photo()}, format="multipart")
    assert r.status_code == 200, r.content
    by_id = {s["student"]: s for s in r.data["suggestions"]}
    assert by_id[st.pk]["has_reference"] is True
    others = [s for sid, s in by_id.items() if sid != st.pk]
    assert others and all(s["has_reference"] is False for s in others)


@pytest.mark.django_db
def test_coach_can_enroll_own_athlete_photo(coach_setup):
    from apps.students.models import Student
    client = APIClient()
    client.force_authenticate(coach_setup["user"])  # attendance.mark, own session
    st = Student.objects.get(student_id="PA-S1")
    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/api/students/reference-photos/",
                    {"student": st.pk, "consent_given": True, "consented_by": "Parent",
                     "image": SimpleUploadedFile("ref.jpg", buf.read(), content_type="image/jpeg")},
                    format="multipart")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_coach_cannot_enroll_other_athlete_photo(coach_setup):
    from datetime import date
    from apps.academy.models import Sport
    from apps.students.models import Student
    sport = Sport.objects.get(code="PAFB")
    outsider = Student.objects.create(student_id="PA-S9", first_name="Out",
                                      enrollment_date=date.today(), sport=sport)
    client = APIClient()
    client.force_authenticate(coach_setup["user"])
    buf = BytesIO()
    Image.new("RGB", (400, 400), "gray").save(buf, format="JPEG")
    buf.seek(0)
    r = client.post("/api/students/reference-photos/",
                    {"student": outsider.pk, "consent_given": True, "consented_by": "Parent",
                     "image": SimpleUploadedFile("ref.jpg", buf.read(), content_type="image/jpeg")},
                    format="multipart")
    assert r.status_code == 403
