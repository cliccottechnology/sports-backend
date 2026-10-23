"""Automatic trainer absence marking + missed-attendance notifications.

Covers: deadline math, idempotency, cancellation/leave/existing-mark skips,
trainer-only (never student) marking, notification delivery, correction
request → approve/reject with audit, config endpoint, and access control.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

TZ = ZoneInfo("Asia/Kolkata")


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)


def mkuser(username, role=None, superuser=False, school=None, pwd="pass12345"):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, is_superuser=superuser, school=school)
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
    cl.force_authenticate(user)
    return cl


@pytest.fixture
def setup():
    from apps.academy.models import School, Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.sessions.models import Session

    school = School.objects.create(name="Auto Academy", code="AUTO")
    sport = Sport.objects.create(name="Auto Sport", code="AUTOSP")
    coach = Coach.objects.create(trainer_id="AUTO-T1", first_name="Auto", last_name="Coach",
                                 school=school)
    coach_user = mkuser("auto_coach", role=role_with("attendance.mark", "attendance.view",
                                                     name="coach"), school=None)
    coach_user.coach_profile = coach
    coach_user.save()
    loc = TrainingLocation.objects.create(name="Auto Loc", school=school)
    batch = Batch.objects.create(name="Auto Batch", sport=sport, coach=coach, school=school,
                                 start_time=time(12, 0), end_time=time(13, 0), location=loc)
    today = datetime.now(TZ).date()
    sess = Session.objects.create(batch=batch, sport=sport, coach=coach, date=today, slot=1,
                                  start_time=time(12, 0), end_time=time(12, 30), location=loc)
    return {"school": school, "coach": coach, "coach_user": coach_user, "batch": batch,
            "session": sess, "today": today, "loc": loc, "sport": sport}


def run(now, **kw):
    from apps.coaches.attendance_automation import process_trainer_attendance
    return process_trainer_attendance(now=now, **kw)


@pytest.mark.django_db
def test_auto_marks_absent_after_deadline_and_notifies(setup):
    from apps.coaches.models import TrainerCheckIn
    from apps.notifications.models import Notification
    admin = mkuser("auto_admin", superuser=True)
    res = run(at(setup["today"], 23, 0))
    assert res["auto_absent"] == 1
    rec = TrainerCheckIn.objects.get(session=setup["session"])
    assert rec.status == "absent" and rec.auto_marked is True
    # trainer + admin both notified
    assert Notification.objects.filter(user=setup["coach_user"]).exists()
    assert Notification.objects.filter(user=admin).exists()


@pytest.mark.django_db
def test_running_twice_is_idempotent(setup):
    from apps.coaches.models import TrainerCheckIn
    run(at(setup["today"], 23, 0))
    res2 = run(at(setup["today"], 23, 30))
    assert res2["auto_absent"] == 0
    assert TrainerCheckIn.objects.filter(session=setup["session"]).count() == 1


@pytest.mark.django_db
def test_not_marked_before_deadline(setup):
    from apps.coaches.models import TrainerCheckIn
    res = run(at(setup["today"], 8, 0))
    assert res["auto_absent"] == 0
    assert res["skipped_not_due"] >= 1
    assert not TrainerCheckIn.objects.filter(session=setup["session"]).exists()


@pytest.mark.django_db
def test_cancelled_session_skipped(setup):
    from apps.coaches.models import TrainerCheckIn
    setup["session"].status = "cancelled"
    setup["session"].save(update_fields=["status"])
    res = run(at(setup["today"], 23, 0))
    assert res["auto_absent"] == 0
    assert not TrainerCheckIn.objects.filter(session=setup["session"]).exists()


@pytest.mark.django_db
def test_existing_mark_never_overwritten(setup):
    from apps.coaches.models import TrainerCheckIn
    TrainerCheckIn.objects.create(coach=setup["coach"], session=setup["session"], status="present")
    res = run(at(setup["today"], 23, 0))
    assert res["auto_absent"] == 0
    assert TrainerCheckIn.objects.get(session=setup["session"]).status == "present"


@pytest.mark.django_db
def test_approved_leave_skipped_pending_leave_does_not(setup):
    from apps.coaches.models import TrainerCheckIn, TrainerLeave
    TrainerLeave.objects.create(coach=setup["coach"], date=setup["today"], reason="sick",
                                status="approved")
    res = run(at(setup["today"], 23, 0))
    assert res["auto_absent"] == 0
    assert res["skipped_leave"] >= 1
    assert not TrainerCheckIn.objects.filter(session=setup["session"]).exists()

    # A pending (not approved) leave must NOT excuse the absence.
    TrainerLeave.objects.all().update(status="pending")
    res2 = run(at(setup["today"], 23, 0))
    assert res2["auto_absent"] == 1


@pytest.mark.django_db
def test_marks_trainer_only_not_students(setup):
    from apps.attendance.models import AttendanceRecord
    run(at(setup["today"], 23, 0))
    assert AttendanceRecord.objects.count() == 0


@pytest.mark.django_db
def test_correction_request_then_admin_approves_with_audit(setup):
    from apps.coaches.models import TrainerAttendanceCorrection, TrainerCheckIn
    from apps.audit.models import AuditLog
    run(at(setup["today"], 23, 0))
    checkin = TrainerCheckIn.objects.get(session=setup["session"])
    assert checkin.auto_marked is True

    coach_cl = auth_client(setup["coach_user"])
    r = coach_cl.post("/api/coaches/corrections/",
                      {"checkin": checkin.pk, "requested_status": "present",
                       "reason": "I took the session; app had no network."}, format="json")
    assert r.status_code == 201, r.content
    corr = TrainerAttendanceCorrection.objects.get(pk=r.data["id"])
    assert corr.status == "pending" and corr.original_status == "absent"

    admin = mkuser("corr_admin", role=role_with("attendance.correct", name="corr_admin_role"))
    admin_cl = auth_client(admin)
    r2 = admin_cl.post(f"/api/coaches/corrections/{corr.pk}/approve/", {}, format="json")
    assert r2.status_code == 200, r2.content
    checkin.refresh_from_db()
    corr.refresh_from_db()
    assert checkin.status == "present" and checkin.auto_marked is False
    assert corr.status == "approved" and corr.original_status == "absent"
    assert corr.reviewed_by_id == admin.pk
    assert AuditLog.objects.filter(action="trainer.correction_approve").exists()


@pytest.mark.django_db
def test_correction_reject_requires_reason_and_keeps_absence(setup):
    from apps.coaches.models import TrainerAttendanceCorrection, TrainerCheckIn
    run(at(setup["today"], 23, 0))
    checkin = TrainerCheckIn.objects.get(session=setup["session"])
    coach_cl = auth_client(setup["coach_user"])
    rid = coach_cl.post("/api/coaches/corrections/",
                        {"checkin": checkin.pk, "requested_status": "present",
                         "reason": "please"}, format="json").data["id"]

    admin_cl = auth_client(mkuser("rej_admin",
                                  role=role_with("attendance.correct", name="rej_admin_role")))
    # Reject without a reason is refused.
    assert admin_cl.post(f"/api/coaches/corrections/{rid}/reject/", {}, format="json").status_code == 400
    r = admin_cl.post(f"/api/coaches/corrections/{rid}/reject/",
                      {"note": "No supporting evidence."}, format="json")
    assert r.status_code == 200, r.content
    checkin.refresh_from_db()
    assert checkin.status == "absent"  # original absence preserved
    assert checkin.auto_marked is True
    assert TrainerAttendanceCorrection.objects.get(pk=rid).status == "rejected"


@pytest.mark.django_db
def test_coach_cannot_directly_edit_auto_marked_checkin(setup):
    from apps.coaches.models import TrainerCheckIn
    run(at(setup["today"], 23, 0))
    checkin = TrainerCheckIn.objects.get(session=setup["session"])
    coach_cl = auth_client(setup["coach_user"])
    r = coach_cl.patch(f"/api/coaches/checkins/{checkin.pk}/", {"status": "present"}, format="json")
    assert r.status_code == 403
    assert TrainerCheckIn.objects.get(pk=checkin.pk).status == "absent"


@pytest.mark.django_db
def test_config_endpoint_get_and_put_toggles_automation(setup):
    from apps.coaches.models import TrainerCheckIn
    admin = mkuser("cfg_admin", superuser=True)
    cl = auth_client(admin)
    got = cl.get("/api/coaches/attendance-config/")
    assert got.status_code == 200, got.content
    keys = {row["key"] for row in got.data["settings"]}
    assert "trainer.auto_absent_enabled" in keys and "trainer.attendance_grace_minutes" in keys

    put = cl.put("/api/coaches/attendance-config/",
                 {"settings": {"trainer.auto_absent_enabled": False}}, format="json")
    assert put.status_code == 200, put.content
    # Disabled → nothing is auto-marked.
    res = run(at(setup["today"], 23, 0), school_id=setup["school"].pk)
    assert res["auto_absent"] == 0
    assert not TrainerCheckIn.objects.filter(session=setup["session"]).exists()
