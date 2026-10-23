"""Trainer marks attendance manually on their own sessions, and it reflects on
the admin portal — including WHO marked it and WHEN.
"""
import pytest
from rest_framework.test import APIClient


def mkuser(username, role=None, superuser=False, school=None, pwd="pass12345"):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, is_superuser=superuser,
             school=school)
    u.set_password(pwd)
    u.save()
    return u


def role_with(*codes, name="trole"):
    from apps.accounts.models import Permission, Role
    r, _ = Role.objects.get_or_create(name=name)
    r.permissions.add(*Permission.objects.filter(code__in=codes))
    return r


def auth_client(user, password="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": user.username, "password": password})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


@pytest.fixture
def setup():
    """Two rostered students and a coach's three sessions for the day."""
    from datetime import date, time

    from apps.academy.models import School, Sport
    from apps.batches.models import Batch, BatchStudent
    from apps.coaches.models import Coach
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student

    school, _ = School.objects.get_or_create(name="Att Academy", code="ATT")
    sport, _ = Sport.objects.get_or_create(name="Att Sport", code="ATTSP")
    coach_user = mkuser("att_coach", role=role_with("attendance.mark", "attendance.view",
                                                    "sessions.view", name="coach"))
    coach = Coach.objects.create(trainer_id="T-ATT-1", first_name="A.", last_name="Coach",
                                 school=school)
    coach_user.coach_profile = coach
    coach_user.save()

    batch = Batch.objects.create(name="Att Batch", sport=sport, school=school,
                                 start_time=time(8, 0), end_time=time(21, 0))
    students = [Student.objects.create(student_id=f"ATT-{i}", first_name=f"S{i}",
                                       last_name="Tudent", enrollment_date=date.today(),
                                       school=school)
                for i in (1, 2)]
    for s in students:
        BatchStudent.objects.create(batch=batch, student=s, is_active=True)

    sessions = []
    for slot in (1, 2, 3):  # a coach's three sessions for the day
        ses = Session.objects.create(batch=batch, sport=sport, coach=coach, date=date.today(),
                                     slot=slot, start_time=time(9 + slot, 0),
                                     end_time=time(10 + slot, 0))
        SessionStudent.objects.bulk_create(
            [SessionStudent(session=ses, student=s) for s in students])
        sessions.append(ses)
    return {"school": school, "coach_user": coach_user, "coach": coach,
            "students": students, "sessions": sessions, "batch": batch}


@pytest.mark.django_db
def test_coach_role_can_mark_attendance_on_migrated_db():
    """Regression: attendance.mark used to be granted only by seed_demo, so a
    real installation left coaches unable to mark anything."""
    from apps.accounts.models import Role
    coach = Role.objects.get(name="coach")
    assert coach.permissions.filter(code="attendance.mark").exists()
    assert coach.permissions.filter(code="attendance.view").exists()


@pytest.mark.django_db
def test_trainer_marks_three_sessions_manually(setup):
    cl = auth_client(setup["coach_user"])
    students = setup["students"]
    marks = ["present", "absent", "late"]  # per student across the day

    for idx, ses in enumerate(setup["sessions"]):
        roster = cl.get(f"/api/attendance/roster/?session={ses.pk}").data["roster"]
        assert len(roster) == 2
        # Nothing marked yet.
        assert all(r["status"] == "not_marked" for r in roster)
        assert all(r["marked_by_name"] == "" for r in roster)

        items = [{"student": students[0].pk, "status": marks[idx]},
                 {"student": students[1].pk, "status": "leave"}]
        r = cl.post("/api/attendance/bulk_mark/", {"session": ses.pk, "items": items},
                    format="json")
        assert r.status_code == 200, r.data
        assert {x["status"] for x in r.data["results"]} <= {"present", "absent", "late", "leave"}
        assert all(x.get("created") is True for x in r.data["results"])

    # All three sessions are now marked for both students.
    for ses in setup["sessions"]:
        roster = cl.get(f"/api/attendance/roster/?session={ses.pk}").data["roster"]
        assert all(r["status"] != "not_marked" for r in roster)
        assert all(r["marked_by_name"] == "att_coach" for r in roster)
        assert all(r["marked_at"] for r in roster)


@pytest.mark.django_db
def test_marks_reflect_to_admin_portal_with_attribution(setup):
    """The admin sees the same rows, and can tell which trainer marked them."""
    admin_user = mkuser("att_admin", role=role_with("attendance.view", name="att_admin_role"))
    assert admin_user.school_id is None

    coach_cl = auth_client(setup["coach_user"])
    ses = setup["sessions"][0]
    students = setup["students"]
    coach_cl.post("/api/attendance/bulk_mark/",
                  {"session": ses.pk,
                   "items": [{"student": students[0].pk, "status": "present"},
                             {"student": students[1].pk, "status": "absent"}]},
                  format="json")

    admin_cl = auth_client(admin_user)
    rows = {r["student"]: r for r in
            admin_cl.get(f"/api/attendance/roster/?session={ses.pk}").data["roster"]}
    assert rows[students[0].pk]["status"] == "present"
    assert rows[students[1].pk]["status"] == "absent"
    assert rows[students[0].pk]["marked_by_name"] == "att_coach"
    assert rows[students[0].pk]["marked_at"]

    # Same attribution on the list endpoint the admin detail/report pages use.
    listed = admin_cl.get("/api/attendance/?session=%d" % ses.pk).data["results"]
    by_student = {a["student"]: a for a in listed}
    assert by_student[students[0].pk]["marked_by_name"] == "att_coach"
    assert by_student[students[0].pk]["marked_by"] == setup["coach_user"].pk


@pytest.mark.django_db
def test_coach_cannot_mark_someone_elses_session(setup):
    from apps.coaches.models import Coach
    from apps.sessions.models import Session, SessionStudent
    from datetime import date, time

    other = Coach.objects.create(trainer_id="T-ATT-2", first_name="B.", last_name="Coach",
                                 school=setup["school"])
    stranger = Session.objects.create(batch=setup["batch"], sport=setup["batch"].sport,
                                      coach=other, date=date.today(), slot=1,
                                      start_time=time(18, 0), end_time=time(19, 0))
    SessionStudent.objects.create(session=stranger, student=setup["students"][0])

    cl = auth_client(setup["coach_user"])
    r = cl.post("/api/attendance/bulk_mark/",
                {"session": stranger.pk,
                 "items": [{"student": setup["students"][0].pk, "status": "present"}]},
                format="json")
    assert r.status_code == 403
    assert "Not your session" in str(r.data)


@pytest.mark.django_db
def test_remarking_creates_audit_trail(setup):
    """An admin correcting a coach's mark keeps the original author and logs why."""
    cl = auth_client(setup["coach_user"])
    ses = setup["sessions"][0]
    st = setup["students"][0]
    cl.post("/api/attendance/bulk_mark/",
            {"session": ses.pk, "items": [{"student": st.pk, "status": "present"}]},
            format="json")

    from apps.accounts.models import Role
    admin_cl = auth_client(mkuser("att_fixer",
                                  role=role_with("attendance.view", "attendance.correct",
                                                 name="att_fixer_role")))
    rec_id = admin_cl.get(f"/api/attendance/?session={ses.pk}&student={st.pk}").data["results"][0]["id"]
    r = admin_cl.patch(f"/api/attendance/{rec_id}/",
                       {"status": "late", "reason": "Arrived late after warm-up"}, format="json")
    assert r.status_code == 200, r.data

    from apps.attendance.models import AttendanceCorrection
    corr = AttendanceCorrection.objects.filter(record_id=rec_id).first()
    assert corr is not None
    assert corr.old_status == "present" and corr.new_status == "late"
    assert corr.corrected_by.username == "att_fixer"

    # Still attributed to the trainer who actually took the register.
    rec = admin_cl.get(f"/api/attendance/{rec_id}/").data
    assert rec["marked_by_name"] == "att_coach"
    assert rec["status"] == "late"
