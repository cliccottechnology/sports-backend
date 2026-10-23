"""Multi-school isolation: scoped visibility, cross-school blocks, lockout, onboarding."""
from datetime import date, time

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def schools():
    from apps.academy.models import School
    a, _ = School.objects.get_or_create(name="T-School-A", defaults={"code": "TSA"})
    b, _ = School.objects.get_or_create(name="T-School-B", defaults={"code": "TSB"})
    return a, b


@pytest.fixture
def school_data(schools):
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch, BatchStudent
    from apps.coaches.models import Coach
    from apps.fees.models import FeePlan, StudentFee
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student
    a, b = schools
    sport, _ = Sport.objects.get_or_create(name="T-SC-Football", defaults={"code": "TSCF"})
    loc, _ = TrainingLocation.objects.get_or_create(name="T-SC-Loc")
    plan, _ = FeePlan.objects.get_or_create(name="T-SC-Plan", cycle="monthly",
                                            defaults={"amount": 1000})
    out = {}
    for sch, tag in ((a, "A"), (b, "B")):
        coach = Coach.objects.create(trainer_id=f"T-SC-{tag}", first_name=tag, phone=tag,
                                     weekly_off_days=[], school=sch)
        batch = Batch.objects.create(name=f"T-SC-{tag}", sport=sport, coach=coach,
                                     start_time=time(7, 0), end_time=time(8, 0), location=loc, school=sch)
        st = Student.objects.create(student_id=f"T-SC-{tag}", first_name=tag,
                                    enrollment_date=date.today(), sport=sport, school=sch)
        BatchStudent.objects.create(batch=batch, student=st)
        sess = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                      start_time=time(7, 0), end_time=time(8, 0), location=loc)
        SessionStudent.objects.create(session=sess, student=st)
        StudentFee.objects.create(student=st, fee_plan=plan, period_label="2026-10",
                                  amount=1000, due_date=date.today())
        out[tag] = {"coach": coach, "batch": batch, "student": st, "session": sess}
    return out


def school_admin(username, school, codes):
    from apps.accounts.models import Permission, Role, User
    role, _ = Role.objects.get_or_create(name=f"role_{username}")
    role.permissions.add(*Permission.objects.filter(code__in=codes))
    u = User(username=username, role=role, school=school, is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": username, "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


VIEWS = ["students.view", "batches.view", "sessions.view", "attendance.view",
         "fees.view", "coaches.view", "parents.view"]


@pytest.mark.django_db
def test_school_admin_sees_only_own(schools, school_data):
    a, b = schools
    cl = school_admin("sa_a", a, VIEWS)
    for url, key in (("/api/students/", "T-SC-A"), ("/api/batches/", "T-SC-A"),
                     ("/api/sessions/", None), ("/api/fees/", None)):
        r = cl.get(url)
        assert r.status_code == 200, (url, r.content)
        rows = r.data.get("results", r.data)
        assert rows, url
        assert all("T-SC-B" not in str(x) for x in rows), url
    got = {s["student_id"] for s in cl.get("/api/students/").data["results"]}
    assert got == {"T-SC-A"}


@pytest.mark.django_db
def test_cross_school_create_rejected(schools, school_data):
    a, b = schools
    cl = school_admin("sa_a2", a, VIEWS + ["students.create"])
    assert cl.post("/api/students/", {"student_id": "T-X", "first_name": "X",
                                      "enrollment_date": str(date.today()), "school": b.pk},
                   format="json").status_code == 403


@pytest.mark.django_db
def test_create_auto_stamped_to_own_school(schools):
    a, _ = schools
    cl = school_admin("sa_a3", a, VIEWS + ["students.create"])
    r = cl.post("/api/students/", {"student_id": "T-AUTO", "first_name": "Auto",
                                   "enrollment_date": str(date.today())}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["school"] == a.pk


@pytest.mark.django_db
def test_cross_school_batch_link_rejected(schools, school_data):
    a, b = schools
    cl = school_admin("sa_a4", a, VIEWS + ["batches.edit", "students.edit"])
    sb = school_data["B"]["batch"]
    st = school_data["A"]["student"]
    r = cl.post("/api/batches/rosters/list/",
                {"batch": sb.pk, "student": st.pk}, format="json")
    assert r.status_code in (400, 403), r.content


@pytest.mark.django_db
def test_inactive_school_locks_out(schools):
    a, _ = schools
    cl = school_admin("sa_a5", a, VIEWS)
    assert cl.get("/api/students/").status_code == 200
    a.is_active = False
    a.save()
    assert cl.get("/api/students/").status_code == 401
    a.is_active = True
    a.save()


@pytest.mark.django_db
def test_super_admin_sees_all_and_onboards(schools, school_data):
    from apps.accounts.models import Role, User
    sa = Role.objects.get(name="super_admin")
    boss = User(username="onboarder", role=sa, is_superuser=True, is_active=True)
    boss.set_password("pass12345")
    boss.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "onboarder", "password": "pass12345"})
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    r = cl.post("/api/schools/", {"name": "T-School-C", "code": "TSC"}, format="json")
    assert r.status_code == 201, r.content
    got = {s["student_id"] for s in cl.get("/api/students/").data["results"]}
    assert {"T-SC-A", "T-SC-B"} <= got
    r = cl.post("/api/schools/", {"name": "T-School-C", "code": "TSC"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_legacy_staff_unscoped(schools, school_data):
    from apps.accounts.models import Permission, Role, User
    role, _ = Role.objects.get_or_create(name="academy_admin")
    role.permissions.add(*Permission.objects.filter(code__in=["students.view", "students.manage"]))
    u = User(username="legacy_all", role=role, is_active=True, school=None)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "legacy_all", "password": "pass12345"})
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    got = {s["student_id"] for s in cl.get("/api/students/").data["results"]}
    assert {"T-SC-A", "T-SC-B"} <= got
