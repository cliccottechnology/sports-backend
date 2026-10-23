"""Weekday/weekend make-up attendance: traceability, approval, capacity,
sport eligibility, and the trainer-salary validation gate.

Acceptance criteria covered here:
- manual attendance (present/absent/late/leave/not_marked) stays the source of truth
- an approved make-up is recorded against the session actually attended and is
  traceable back to the missed session; the original absence is never flipped
- make-ups do not duplicate records or auto-credit trainer salary
- trainer earnings come only from completed, validated sessions × per-session rate
"""
from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient


def _admin():
    from apps.accounts.models import User
    u = User(username="mk_admin", is_active=True, is_superuser=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.fixture
def mk():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach, CoachSport
    from apps.students.models import Student
    from apps.sessions.models import Session, SessionStudent
    from apps.attendance.models import AttendanceRecord

    sport = Sport.objects.create(name="MK-Football", code="MKFB")
    other_sport = Sport.objects.create(name="MK-Cricket", code="MKCR")
    coach = Coach.objects.create(trainer_id="MK-T1", first_name="Kumar", phone="1", weekly_off_days=[])
    CoachSport.objects.create(coach=coach, sport=sport)
    loc = TrainingLocation.objects.create(name="MK-Loc")
    batch = Batch.objects.create(name="MK-B", sport=sport, coach=coach, capacity=2,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    student = Student.objects.create(student_id="MK-S1", first_name="Ravi", enrollment_date=date.today(), sport=sport)
    other = Student.objects.create(student_id="MK-S2", first_name="Cric", enrollment_date=date.today(), sport=other_sport)

    # A missed weekday session (student was absent) and a weekend make-up session.
    missed = Session.objects.create(sport=sport, batch=batch, coach=coach,
                                    date=date.today() - timedelta(days=3),
                                    start_time=time(7, 0), end_time=time(8, 0), location=loc,
                                    status="completed")
    SessionStudent.objects.create(session=missed, student=student)
    AttendanceRecord.objects.create(session=missed, student=student, status="absent")

    makeup = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                    start_time=time(7, 0), end_time=time(8, 0), location=loc,
                                    status="scheduled")
    return {"sport": sport, "other_sport": other_sport, "coach": coach, "batch": batch,
            "student": student, "other": other, "missed": missed, "makeup": makeup}


@pytest.fixture
def coach_client(mk):
    from apps.accounts.models import Permission, Role, User
    perm, _ = Permission.objects.get_or_create(code="attendance.mark",
                                               defaults={"name": "mark", "module": "attendance"})
    role, _ = Role.objects.get_or_create(name="coach")
    role.permissions.add(perm)
    u = User(username="mk_coach", role=role, is_active=True, coach_profile=mk["coach"])
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.mark.django_db
def test_admin_adds_student_for_makeup_traceable(mk):
    admin = _admin()
    r = admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
                   {"student": mk["student"].pk, "makeup_for": mk["missed"].pk}, format="json")
    assert r.status_code == 201, r.content
    from apps.sessions.models import SessionStudent
    assert SessionStudent.objects.filter(session=mk["makeup"], student=mk["student"]).exists()
    mk["makeup"].refresh_from_db()
    assert mk["makeup"].is_makeup is True
    assert mk["makeup"].makeup_for_id == mk["missed"].pk


@pytest.mark.django_db
def test_makeup_requires_admin_approval_for_trainer(mk, coach_client):
    # Default policy: make-up attendance requires approval.
    r = coach_client.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
                          {"student": mk["student"].pk, "makeup_for": mk["missed"].pk}, format="json")
    assert r.status_code == 403, r.content
    # Turning the policy off lets the trainer self-serve.
    from apps.academy.models import AcademySetting
    AcademySetting.objects.create(key="attendance.makeup_requires_approval", value=False)
    r2 = coach_client.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
                           {"student": mk["student"].pk, "makeup_for": mk["missed"].pk}, format="json")
    assert r2.status_code == 201, r2.content


@pytest.mark.django_db
def test_makeup_rejects_other_sport_and_capacity(mk):
    admin = _admin()
    # Student not enrolled in the session's sport.
    r = admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
                   {"student": mk["other"].pk, "makeup_for": mk["missed"].pk}, format="json")
    assert r.status_code == 400, r.content
    # Capacity: batch capacity is 2 — add two students, a third is rejected.
    from apps.students.models import Student
    from apps.sessions.models import SessionStudent
    s2 = Student.objects.create(student_id="MK-S3", first_name="Two", enrollment_date=date.today(), sport=mk["sport"])
    s3 = Student.objects.create(student_id="MK-S4", first_name="Three", enrollment_date=date.today(), sport=mk["sport"])
    assert admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/", {"student": s2.pk}, format="json").status_code == 201
    assert admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/", {"student": s3.pk}, format="json").status_code == 201
    s4 = Student.objects.create(student_id="MK-S5", first_name="Four", enrollment_date=date.today(), sport=mk["sport"])
    assert admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/", {"student": s4.pk}, format="json").status_code == 400


@pytest.mark.django_db
def test_makeup_attendance_separate_from_absence(mk, coach_client):
    admin = _admin()
    admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
               {"student": mk["student"].pk, "makeup_for": mk["missed"].pk}, format="json")
    r = coach_client.post("/api/attendance/bulk_mark/", {
        "session": mk["makeup"].pk,
        "items": [{"student": mk["student"].pk, "status": "present", "makeup_for": mk["missed"].pk}],
    }, format="json")
    assert r.status_code == 200, r.content
    from apps.attendance.models import AttendanceRecord
    makeup_rec = AttendanceRecord.objects.get(session=mk["makeup"], student=mk["student"])
    missed_rec = AttendanceRecord.objects.get(session=mk["missed"], student=mk["student"])
    assert makeup_rec.status == "present" and makeup_rec.makeup_for_id == mk["missed"].pk
    # Original absence is untouched and the two records are distinct rows.
    assert missed_rec.status == "absent"
    assert missed_rec.makeup_for_id is None
    assert AttendanceRecord.objects.filter(student=mk["student"]).count() == 2


@pytest.mark.django_db
def test_makeup_does_not_auto_credit_trainer_salary(mk):
    from apps.trainer_fees.models import TrainerFeeRule
    from apps.trainer_fees.engine import compute_payables_for_session
    TrainerFeeRule.objects.create(trainer=mk["coach"], rate_per_session=500,
                                  effective_from=date.today() - timedelta(days=30), is_active=True)
    # Complete the make-up session: still no payable until an admin validates it.
    mk["makeup"].status = "completed"
    mk["makeup"].is_makeup = True
    mk["makeup"].makeup_for = mk["missed"]
    mk["makeup"].save()
    assert compute_payables_for_session(mk["makeup"]) is None
    mk["makeup"].validated_for_trainer_fee = True
    mk["makeup"].save(update_fields=["validated_for_trainer_fee"])
    assert compute_payables_for_session(mk["makeup"]) is not None


@pytest.mark.django_db
def test_regular_completed_session_is_eligible_by_default(mk):
    from apps.trainer_fees.models import TrainerFeeRule
    from apps.trainer_fees.engine import compute_payables_for_session
    TrainerFeeRule.objects.create(trainer=mk["coach"], rate_per_session=500,
                                  effective_from=date.today() - timedelta(days=30), is_active=True)
    s = mk["makeup"]
    s.status = "completed"
    s.save(update_fields=["status"])
    assert compute_payables_for_session(s) is not None


@pytest.mark.django_db
def test_validation_gate_setting_requires_validation_for_all(mk):
    from apps.academy.models import AcademySetting
    from apps.trainer_fees.models import TrainerFeeRule
    from apps.trainer_fees.engine import compute_payables_for_session
    AcademySetting.objects.create(key="trainer.require_session_validation", value=True)
    TrainerFeeRule.objects.create(trainer=mk["coach"], rate_per_session=500,
                                  effective_from=date.today() - timedelta(days=30), is_active=True)
    s = mk["makeup"]
    s.status = "completed"
    s.save(update_fields=["status"])
    assert compute_payables_for_session(s) is None
    s.validated_for_trainer_fee = True
    s.save(update_fields=["validated_for_trainer_fee"])
    assert compute_payables_for_session(s) is not None


@pytest.mark.django_db
def test_validate_for_fee_endpoint(mk):
    admin = _admin()
    r = admin.post(f"/api/sessions/{mk['makeup'].pk}/validate_for_fee/", {"validated": True}, format="json")
    assert r.status_code == 200, r.content
    mk["makeup"].refresh_from_db()
    assert mk["makeup"].validated_for_trainer_fee is True


@pytest.mark.django_db
def test_makeup_history_report(mk, coach_client):
    admin = _admin()
    admin.post(f"/api/sessions/{mk['makeup'].pk}/add_student/",
               {"student": mk["student"].pk, "makeup_for": mk["missed"].pk}, format="json")
    coach_client.post("/api/attendance/bulk_mark/", {
        "session": mk["makeup"].pk,
        "items": [{"student": mk["student"].pk, "status": "present", "makeup_for": mk["missed"].pk}],
    }, format="json")
    r = admin.get("/api/reports/attendance/makeup/")
    assert r.status_code == 200, r.content
    rows = r.data["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["student"] == mk["student"].student_id
    assert row["missed_status"] == "absent"
    assert row["makeup_status"] == "present"
    assert row["missed_session"] == mk["missed"].pk
    assert row["makeup_session"] == mk["makeup"].pk
