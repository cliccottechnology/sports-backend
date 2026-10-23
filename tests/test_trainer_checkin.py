"""Trainer self check-in: own sessions only, one per session, duplicates blocked."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def checkin_setup():
    from datetime import date, time
    from apps.academy.models import Sport, TrainingLocation
    from apps.accounts.models import Permission, Role, User
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student
    sport = Sport.objects.create(name="K-Football", code="KFB")
    coach = Coach.objects.create(trainer_id="K-T1", first_name="Kiran", phone="3", weekly_off_days=[])
    other = Coach.objects.create(trainer_id="K-T2", first_name="Mohan", phone="4", weekly_off_days=[])
    loc = TrainingLocation.objects.create(name="K-Loc")
    batch = Batch.objects.create(name="K-B", sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    st = Student.objects.create(student_id="K-S1", first_name="A", enrollment_date=date.today(), sport=sport)
    sess = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                  start_time=time(7, 0), end_time=time(8, 0), location=loc)
    SessionStudent.objects.create(session=sess, student=st)
    other_sess = Session.objects.create(sport=sport, batch=batch, coach=other, date=date.today(),
                                        start_time=time(9, 0), end_time=time(10, 0), location=loc)
    perm, _ = Permission.objects.get_or_create(code="attendance.mark",
                                               defaults={"name": "mark", "module": "attendance"})
    role, _ = Role.objects.get_or_create(name="coach")
    role.permissions.add(perm)
    user = User(username="checkin_coach", role=role, is_active=True, coach_profile=coach)
    user.set_password("pass12345")
    user.save()
    return {"user": user, "session": sess, "other_session": other_sess}


@pytest.mark.django_db
def test_checkin_own_session(checkin_setup):
    c = APIClient()
    c.force_authenticate(checkin_setup["user"])
    r = c.post("/api/coaches/checkins/", {"session": checkin_setup["session"].pk}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["status"] == "present"
    mine = c.get("/api/coaches/checkins/")
    assert mine.status_code == 200
    assert any(x["session"] == checkin_setup["session"].pk for x in mine.data["results"])


@pytest.mark.django_db
def test_checkin_duplicate_blocked(checkin_setup):
    c = APIClient()
    c.force_authenticate(checkin_setup["user"])
    url = "/api/coaches/checkins/"
    assert c.post(url, {"session": checkin_setup["session"].pk}, format="json").status_code == 201
    r2 = c.post(url, {"session": checkin_setup["session"].pk}, format="json")
    assert r2.status_code == 400


@pytest.mark.django_db
def test_checkin_other_coach_session_forbidden(checkin_setup):
    c = APIClient()
    c.force_authenticate(checkin_setup["user"])
    r = c.post("/api/coaches/checkins/", {"session": checkin_setup["other_session"].pk}, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_coach_can_correct_own_checkin(checkin_setup):
    from apps.coaches.models import TrainerCheckIn
    c = APIClient()
    c.force_authenticate(checkin_setup["user"])
    r = c.post("/api/coaches/checkins/",
               {"session": checkin_setup["session"].pk, "status": "absent"}, format="json")
    assert r.status_code == 201, r.content
    cid = r.data["id"]
    r2 = c.patch(f"/api/coaches/checkins/{cid}/", {"status": "present"}, format="json")
    assert r2.status_code == 200, r2.content
    assert r2.data["status"] == "present"
    assert TrainerCheckIn.objects.get(pk=cid).status == "present"


@pytest.mark.django_db
def test_coach_cannot_edit_other_coachs_checkin(checkin_setup):
    from apps.coaches.models import Coach, TrainerCheckIn
    other = Coach.objects.get(trainer_id="K-T2")
    rec = TrainerCheckIn.objects.create(coach=other, session=checkin_setup["other_session"], status="present")
    c = APIClient()
    c.force_authenticate(checkin_setup["user"])
    r = c.patch(f"/api/coaches/checkins/{rec.pk}/", {"status": "absent"}, format="json")
    assert r.status_code in (403, 404)


@pytest.mark.django_db
def test_admin_lists_checkins_with_coach_name_and_date_filter(checkin_setup):
    from apps.accounts.models import Permission, Role, User
    from apps.coaches.models import Coach, TrainerCheckIn
    TrainerCheckIn.objects.create(coach=Coach.objects.get(trainer_id="K-T1"),
                                  session=checkin_setup["session"], status="present")
    role, _ = Role.objects.get_or_create(name="att_admin")
    role.permissions.add(*Permission.objects.filter(code__in=["coaches.view"]))
    admin = User.objects.create_user(username="att_admin", password="pass12345", role=role)
    c = APIClient()
    c.force_authenticate(admin)
    today = checkin_setup["session"].date.isoformat()
    r = c.get("/api/coaches/checkins/", {"date_from": today, "date_to": today})
    assert r.status_code == 200, r.content
    row = r.data["results"][0]
    assert row["coach_name"] == "Kiran"
    assert row["trainer_id"] == "K-T1"
    assert row["session_date"] == today
    # A date window that excludes the session returns nothing.
    empty = c.get("/api/coaches/checkins/", {"date_from": "2000-01-01", "date_to": "2000-01-02"})
    assert empty.data["results"] == []


@pytest.mark.django_db
def test_admin_checkin_list_is_school_scoped(checkin_setup):
    from apps.academy.models import School
    from apps.accounts.models import Permission, Role, User
    from apps.coaches.models import Coach, TrainerCheckIn
    a = School.objects.create(name="TA Academy", code="TAA")
    b = School.objects.create(name="TB Academy", code="TAB")
    sess = checkin_setup["session"]
    sess.batch.school = a
    sess.batch.save(update_fields=["school"])
    TrainerCheckIn.objects.create(coach=Coach.objects.get(trainer_id="K-T1"), session=sess, status="present")
    role, _ = Role.objects.get_or_create(name="ta_admin")
    role.permissions.add(*Permission.objects.filter(code__in=["coaches.view"]))
    ua = User.objects.create_user(username="ta_admin_a", password="pass12345", role=role, school=a)
    ub = User.objects.create_user(username="ta_admin_b", password="pass12345", role=role, school=b)
    ca = APIClient(); ca.force_authenticate(ua)
    cb = APIClient(); cb.force_authenticate(ub)
    assert len(ca.get("/api/coaches/checkins/").data["results"]) == 1
    assert cb.get("/api/coaches/checkins/").data["results"] == []
