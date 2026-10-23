"""Advanced features: double-booking guard, alternatives, certificates,
broadcast targeting, achievements, chart data. All live records, no mocks."""
from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def club():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach, CoachSport
    football = Sport.objects.create(name="A-Football", code="AFB")
    cricket = Sport.objects.create(name="A-Cricket", code="ACR")
    loc = TrainingLocation.objects.create(name="A-Loc")
    kumar = Coach.objects.create(trainer_id="A-KUMAR", first_name="Kumar", phone="11", weekly_off_days=[])
    alt = Coach.objects.create(trainer_id="A-ALT", first_name="Alt", phone="22", weekly_off_days=[])
    CoachSport.objects.create(coach=kumar, sport=football)
    CoachSport.objects.create(coach=kumar, sport=cricket)
    CoachSport.objects.create(coach=alt, sport=cricket)
    batch = Batch.objects.create(name="A-B", sport=football, coach=kumar,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    return {"football": football, "cricket": cricket, "loc": loc,
            "kumar": kumar, "alt": alt, "batch": batch}


@pytest.fixture
def admin_client():
    from apps.accounts.models import User
    u = User(username="adv_admin", is_active=True, is_superuser=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


def _mk_session(cl, batch, sport, coach, day, start, end, status="scheduled"):
    r = cl.post("/api/sessions/", {
        "sport": sport.pk, "batch": batch.pk, "coach": coach.pk,
        "date": day.isoformat(), "start_time": start, "end_time": end,
        "status": status, "slot": 1,
    }, format="json")
    return r


@pytest.mark.django_db
def test_4_double_booking_rejected_with_clear_message(admin_client, club):
    c = club
    day = date.today() + timedelta(days=3)
    r1 = _mk_session(admin_client, c["batch"], c["football"], c["kumar"], day, "16:00", "17:00")
    assert r1.status_code == 201, r1.content
    r2 = _mk_session(admin_client, c["batch"], c["cricket"], c["kumar"], day, "16:30", "17:30")
    assert r2.status_code == 400, r2.content
    msg = str(r2.data)
    assert "Kumar" in msg and "04:00 PM" in msg and "05:00 PM" in msg
    assert "another available coach" in msg


@pytest.mark.django_db
def test_5_alternatives_suggest_only_eligible(admin_client, club):
    c = club
    day = (date.today() + timedelta(days=3)).isoformat()
    _mk_session(admin_client, c["batch"], c["football"], c["kumar"],
                date.today() + timedelta(days=3), "16:00", "17:00")
    r = admin_client.get("/api/sessions/available_coaches/",
                         {"sport": c["cricket"].pk, "date": day,
                          "start_time": "16:30", "end_time": "17:30"})
    assert r.status_code == 200, r.content
    ids = [a["id"] for a in r.data["available"]]
    assert c["alt"].pk in ids  # qualified + free
    assert c["kumar"].pk not in ids  # busy overlapping


@pytest.mark.django_db
def test_8_eligibility_milestone_and_states(admin_client, club):
    from apps.students.models import Student
    old = Student.objects.create(student_id="A-ARJUN", first_name="Arjun",
                                 sport=club["football"],
                                 enrollment_date=date.today() - timedelta(days=100),
                                 status="active")
    new = Student.objects.create(student_id="A-NEW", first_name="New",
                                 sport=club["football"],
                                 enrollment_date=date.today() - timedelta(days=10),
                                 status="active")
    r = admin_client.get("/api/students/certificates/eligibility/")
    assert r.status_code == 200, r.content
    rows = {x["student_id"]: x for x in r.data["results"]}
    assert rows["A-ARJUN"]["state"] == "eligible"
    # milestone = enrollment + 3 calendar months (not exactly 90 days)
    assert rows["A-ARJUN"]["milestone_date"] > str(date.today() - timedelta(days=20))
    assert rows["A-NEW"]["state"] in ("training", "approaching")


@pytest.mark.django_db
def test_9_policy_thresholds_and_10_issue_flow(admin_client, club):
    from apps.academy.models import AcademySetting
    from apps.attendance.models import AttendanceRecord
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student, StudentCertificate
    st = Student.objects.create(student_id="A-POL", first_name="Pol",
                                sport=club["football"],
                                enrollment_date=date.today() - timedelta(days=120),
                                status="active")
    sess = Session.objects.create(sport=club["football"], batch=club["batch"], coach=club["kumar"],
                                  date=date.today() - timedelta(days=60), status="completed",
                                  start_time=time(7, 0), end_time=time(8, 0), location=club["loc"])
    SessionStudent.objects.create(session=sess, student=st)
    AttendanceRecord.objects.create(session=sess, student=st, status="absent")
    # Strict policy first: 50% attendance required -> review, not eligible
    AcademySetting.objects.update_or_create(key="certificate.min_attendance_pct",
                                            defaults={"value": 50})
    r = admin_client.get("/api/students/certificates/eligibility/", {"state": "eligible"})
    assert all(x["student_id"] != "A-POL" for x in r.data["results"])
    AcademySetting.objects.filter(key="certificate.min_attendance_pct").delete()
    # Issue -> snapshot + PDF + unique number, no duplicates
    r = admin_client.post("/api/students/certificates/issue/", {"student": st.pk}, format="json")
    assert r.status_code == 201, r.content
    cert = StudentCertificate.objects.get(pk=r.data["id"])
    assert cert.sessions_completed == 1
    assert cert.certificate_no.startswith("CERT-")
    assert cert.file and cert.file.size > 0
    assert r.data["notified"]["inbox"] == 0  # no linked parents: nothing to notify
    r2 = admin_client.post("/api/students/certificates/issue/", {"student": st.pk}, format="json")
    assert r2.status_code == 400  # duplicate blocked


@pytest.mark.django_db
def test_6_broadcast_targeting_and_consent(admin_client, club):
    from apps.accounts.models import Role, User
    from apps.notifications.models import Notification
    from apps.parents.models import Parent, StudentParent
    from apps.students.models import Student
    from apps.whatsapp.models import WhatsAppMessage
    st = Student.objects.create(student_id="A-BC", first_name="Bc", sport=club["football"],
                                enrollment_date=date.today(), status="active")
    p_wa = Parent.objects.create(first_name="Wa", relationship="mother", mobile="9000000011",
                                 communication_preference="whatsapp")
    p_sms = Parent.objects.create(first_name="Sms", relationship="father", mobile="9000000022",
                                  communication_preference="sms")
    StudentParent.objects.create(student=st, parent=p_wa)
    StudentParent.objects.create(student=st, parent=p_sms)
    role, _ = Role.objects.get_or_create(name="coach")
    coach_user = User(username="bc_coach", role=role, is_active=True, coach_profile=club["kumar"])
    coach_user.set_password("p")
    coach_user.save()
    r = admin_client.post("/api/notifications/broadcast/", {
        "title": "Training reminder", "message": "Football tomorrow at 5 PM.",
        "roles": ["coach"], "whatsapp": True,
    }, format="json")
    assert r.status_code == 200, r.content
    assert r.data["inbox"] >= 1
    assert Notification.objects.filter(user=coach_user).exists()
    # WhatsApp went only to the consenting parent
    assert WhatsAppMessage.objects.filter(to_number__endswith="000011").exists()
    assert not WhatsAppMessage.objects.filter(to_number__endswith="000022").exists()
    assert r.data["wa_sent"] == 1 and r.data["wa_skipped"] == 1


@pytest.mark.django_db
def test_achievements_crud_and_visibility(admin_client, club):
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    buf.seek(0)
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = admin_client.post("/api/sports/achievements/", {
        "title": "U-14 district champions", "date": str(date.today()),
        "sport": club["football"].pk, "description": "Won 3-1.",
        "photo": SimpleUploadedFile("win.png", buf.read(), content_type="image/png"),
    }, format="multipart")
    assert r.status_code == 201, r.content
    r = admin_client.get("/api/sports/achievements/")
    assert any(x["title"] == "U-14 district champions" for x in r.data["results"])


@pytest.mark.django_db
def test_11_attendance_chart_data_is_real(admin_client, club):
    from apps.attendance.models import AttendanceRecord
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student
    st = Student.objects.create(student_id="A-CH", first_name="Ch",
                                enrollment_date=date.today(), status="active")
    st2 = Student.objects.create(student_id="A-CH2", first_name="Ch2",
                                 enrollment_date=date.today(), status="active")
    sess = Session.objects.create(sport=club["football"], batch=club["batch"], coach=club["kumar"],
                                  date=date.today(), status="completed",
                                  start_time=time(7, 0), end_time=time(8, 0), location=club["loc"])
    SessionStudent.objects.create(session=sess, student=st)
    SessionStudent.objects.create(session=sess, student=st2)
    AttendanceRecord.objects.create(session=sess, student=st, status="present")
    AttendanceRecord.objects.create(session=sess, student=st2, status="leave")
    r = admin_client.get("/api/reports/dashboard/")
    assert r.status_code == 200, r.content
    att = r.data["attendance"]
    assert att["present"] >= 1
    assert att["present"] + att["absent"] + att.get("leave", 0) + att.get("late", 0) >= att["marked"]
