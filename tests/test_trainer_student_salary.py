"""Student-based trainer salary: bands, per-student rate, method changes.

Complements test_trainer_salary.py (session-based). Student-based payables are
snapshotted monthly from eligible students, priced by either count bands or a
flat per-student rate.
"""
from datetime import date, time

import pytest
from rest_framework.test import APIClient


def mkuser(username, role=None, superuser=False, school=None, coach=None, pwd="pass12345"):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, is_superuser=superuser,
             school=school, coach_profile=coach)
    u.set_password(pwd)
    u.save()
    return u


@pytest.fixture
def admin_client():
    u = mkuser("stu_sal_admin", superuser=True)
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.fixture
def base():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    sport = Sport.objects.create(name="SS-Football", code="SSFB")
    loc = TrainingLocation.objects.create(name="SS-Loc")
    coach = Coach.objects.create(trainer_id="SS-T1", first_name="Sam", phone="1",
                                 weekly_off_days=[])
    batch = Batch.objects.create(name="SS-B", sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    return {"sport": sport, "loc": loc, "coach": coach, "batch": batch}


def _add_students(batch, n, *, prefix="SS", status="active", sport=None):
    from apps.batches.models import BatchStudent
    from apps.students.models import Student
    out = []
    for i in range(n):
        s = Student.objects.create(student_id=f"{prefix}-{i:03d}", first_name=f"S{i}",
                                   enrollment_date=date(2026, 9, 1), status=status, sport=sport)
        BatchStudent.objects.create(batch=batch, student=s, is_active=True)
        out.append(s)
    return out


def _band_config(cl, coach, bands, **extra):
    payload = {"trainer": coach.pk, "method": "student_based", "student_mode": "banded",
               "effective_from": "2026-10-01", "bands": bands}
    payload.update(extra)
    r = cl.post("/api/trainer-fees/configs/", payload, format="json")
    assert r.status_code == 201, r.content
    return r.data


def _per_student_config(cl, coach, rate, **extra):
    payload = {"trainer": coach.pk, "method": "student_based", "student_mode": "per_student",
               "student_rate": rate, "effective_from": "2026-10-01"}
    payload.update(extra)
    r = cl.post("/api/trainer-fees/configs/", payload, format="json")
    assert r.status_code == 201, r.content
    return r.data


def _generate(cl, coach, frm="2026-10-01", to="2026-10-31"):
    r = cl.post("/api/trainer-fees/payables/generate/",
                {"trainer": coach.pk, "from_date": frm, "to_date": to}, format="json")
    assert r.status_code == 200, r.content
    return r.data


def _student_payable(coach):
    from apps.trainer_fees.models import TrainerPayable
    return TrainerPayable.objects.filter(trainer=coach, kind="student_salary").first()


BANDS = [{"min_students": 1, "max_students": 25, "amount": "5000"},
         {"min_students": 26, "max_students": 50, "amount": "10000"}]


@pytest.mark.django_db
def test_b_fifty_students_band_10000(admin_client, base):
    """Test B: 50 eligible students fall in the 26-50 band -> Rs 10,000."""
    _band_config(admin_client, base["coach"], BANDS)
    _add_students(base["batch"], 50)
    res = _generate(admin_client, base["coach"])
    assert res["student_salaries"] == 1
    p = _student_payable(base["coach"])
    assert p is not None
    assert p.student_count == 50
    assert float(p.amount) == 10000.0
    assert p.period_start == date(2026, 10, 1)


@pytest.mark.django_db
def test_per_student_rate(admin_client, base):
    """10 active students x Rs 300 = Rs 3,000."""
    _per_student_config(admin_client, base["coach"], "300")
    _add_students(base["batch"], 10)
    _generate(admin_client, base["coach"])
    p = _student_payable(base["coach"])
    assert p.student_count == 10
    assert float(p.amount) == 3000.0


@pytest.mark.django_db
def test_zero_students_no_payable(admin_client, base):
    _band_config(admin_client, base["coach"], BANDS)
    res = _generate(admin_client, base["coach"])
    assert res["student_salaries"] == 0
    assert _student_payable(base["coach"]) is None


@pytest.mark.django_db
def test_only_active_students_counted(admin_client, base):
    _per_student_config(admin_client, base["coach"], "500")
    _add_students(base["batch"], 4, prefix="OK")
    _add_students(base["batch"], 3, prefix="NO", status="inactive")
    _generate(admin_client, base["coach"])
    p = _student_payable(base["coach"])
    assert p.student_count == 4
    assert float(p.amount) == 2000.0


@pytest.mark.django_db
def test_student_counted_once_across_batches(admin_client, base):
    """A student in two batches the trainer coaches counts once."""
    from apps.batches.models import Batch, BatchStudent
    from apps.students.models import Student
    _per_student_config(admin_client, base["coach"], "500")
    b2 = Batch.objects.create(name="SS-B2", sport=base["sport"], coach=base["coach"],
                              start_time=time(9, 0), end_time=time(10, 0), location=base["loc"])
    s = Student.objects.create(student_id="DUP-1", first_name="Dup",
                               enrollment_date=date(2026, 9, 1), status="active")
    BatchStudent.objects.create(batch=base["batch"], student=s, is_active=True)
    BatchStudent.objects.create(batch=b2, student=s, is_active=True)
    _generate(admin_client, base["coach"])
    assert _student_payable(base["coach"]).student_count == 1


@pytest.mark.django_db
def test_snapshot_survives_roster_change(admin_client, base):
    """Regenerating after roster growth never rewrites the frozen count/amount."""
    _per_student_config(admin_client, base["coach"], "500")
    _add_students(base["batch"], 4)
    _generate(admin_client, base["coach"])
    p = _student_payable(base["coach"])
    assert p.student_count == 4 and float(p.amount) == 2000.0
    _add_students(base["batch"], 6, prefix="LATE")
    _generate(admin_client, base["coach"])
    p.refresh_from_db()
    assert p.student_count == 4 and float(p.amount) == 2000.0


@pytest.mark.django_db
def test_above_top_band_cap(admin_client, base):
    _band_config(admin_client, base["coach"], BANDS, above_top_band="cap")
    _add_students(base["batch"], 60)
    _generate(admin_client, base["coach"])
    p = _student_payable(base["coach"])
    assert p.student_count == 60
    assert float(p.amount) == 10000.0


@pytest.mark.django_db
def test_above_top_band_per_student(admin_client, base):
    _band_config(admin_client, base["coach"], BANDS, above_top_band="per_student",
                 student_rate="200")
    _add_students(base["batch"], 60)
    _generate(admin_client, base["coach"])
    p = _student_payable(base["coach"])
    assert p.student_count == 60
    assert float(p.amount) == 12000.0


@pytest.mark.django_db
def test_above_top_band_reject(admin_client, base):
    _band_config(admin_client, base["coach"], BANDS, above_top_band="reject",
                 student_rate="200")
    _add_students(base["batch"], 60)
    res = _generate(admin_client, base["coach"])
    assert res["student_salaries"] == 0
    assert _student_payable(base["coach"]) is None


@pytest.mark.django_db
def test_open_top_band_has_no_cap(admin_client, base):
    bands = [{"min_students": 1, "max_students": 25, "amount": "5000"},
             {"min_students": 26, "max_students": None, "amount": "10000"}]
    _band_config(admin_client, base["coach"], bands)
    _add_students(base["batch"], 60)
    _generate(admin_client, base["coach"])
    assert float(_student_payable(base["coach"]).amount) == 10000.0


@pytest.mark.django_db
def test_overlapping_active_method_rejected(admin_client, base):
    _band_config(admin_client, base["coach"], [{"min_students": 1, "max_students": 50, "amount": "10000"}])
    r = admin_client.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "student_based", "student_mode": "per_student",
        "student_rate": "100", "effective_from": "2026-10-15"}, format="json")
    assert r.status_code == 400, r.content


@pytest.mark.django_db
def test_method_change_after_closing_prior_period(admin_client, base):
    from apps.trainer_fees.models import TrainerSalaryConfig
    cfg = _band_config(admin_client, base["coach"], [{"min_students": 1, "max_students": 50, "amount": "10000"}])
    c = TrainerSalaryConfig.objects.get(pk=cfg["id"])
    c.effective_to = date(2026, 10, 14)
    c.save(update_fields=["effective_to"])
    r = admin_client.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "session_based", "effective_from": "2026-10-15"},
        format="json")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_band_overlap_rejected(admin_client, base):
    r = admin_client.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "student_based", "student_mode": "banded",
        "effective_from": "2026-10-01",
        "bands": [{"min_students": 1, "max_students": 30, "amount": "5000"},
                  {"min_students": 20, "max_students": 50, "amount": "10000"}]}, format="json")
    assert r.status_code == 400, r.content


@pytest.mark.django_db
def test_banded_requires_bands(admin_client, base):
    r = admin_client.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "student_based", "student_mode": "banded",
        "effective_from": "2026-10-01"}, format="json")
    assert r.status_code == 400, r.content


@pytest.mark.django_db
def test_method_change_effective_dates_switch_calculation(admin_client, base):
    """Session-based until Oct 14, student-based after — October prices by session."""
    from apps.trainer_fees.engine import active_config
    from apps.trainer_fees.models import TrainerSalaryConfig
    _band_config(admin_client, base["coach"], [{"min_students": 1, "max_students": 50, "amount": "10000"}])
    TrainerSalaryConfig.objects.filter(trainer=base["coach"]).update(effective_to=date(2026, 10, 14))
    admin_client.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "session_based", "effective_from": "2026-10-15"},
        format="json")
    assert active_config(base["coach"].pk, date(2026, 10, 5)).method == "student_based"
    assert active_config(base["coach"].pk, date(2026, 10, 20)).method == "session_based"


@pytest.mark.django_db
def test_unauthorized_cannot_create_config(base):
    from apps.accounts.models import Role
    from apps.trainer_fees.models import TrainerSalaryConfig
    role, _ = Role.objects.get_or_create(name="coach")
    cl = APIClient()
    cl.force_authenticate(mkuser("stu_coach", role=role, coach=base["coach"]))
    r = cl.post("/api/trainer-fees/configs/", {
        "trainer": base["coach"].pk, "method": "student_based", "student_mode": "per_student",
        "student_rate": "100", "effective_from": "2026-10-01"}, format="json")
    assert r.status_code == 403, r.content
    assert TrainerSalaryConfig.objects.count() == 0


@pytest.mark.django_db
def test_config_scoped_to_school(admin_client, base):
    """A school-bound admin sees only their school's salary configs."""
    from apps.academy.models import School
    from apps.accounts.models import Role
    from apps.coaches.models import Coach
    s1 = School.objects.create(name="SS-Academy-1", code="SSA1")
    s2 = School.objects.create(name="SS-Academy-2", code="SSA2")
    base["coach"].school = s1
    base["coach"].save(update_fields=["school"])
    c2 = Coach.objects.create(trainer_id="SS-T2", first_name="Two", phone="2",
                              weekly_off_days=[], school=s2)
    _per_student_config(admin_client, base["coach"], "100")
    _per_student_config(admin_client, c2, "100")
    from apps.accounts.models import Permission
    role, _ = Role.objects.get_or_create(name="school_admin2")
    role.permissions.add(*Permission.objects.filter(code__in=["trainer_fees.view"]))
    admin = mkuser("ss_admin_1", role=role, school=s1)
    cl = APIClient()
    cl.force_authenticate(admin)
    rows = cl.get("/api/trainer-fees/configs/").data
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert len(rows) == 1
    assert rows[0]["trainer"] == base["coach"].pk
