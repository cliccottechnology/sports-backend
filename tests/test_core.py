"""Backend tests: auth, RBAC, attendance, fees, trainer fees, payment verification."""
import pytest
from django.urls import reverse
from rest_framework.test import APIClient


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
def test_login_and_rbac(client):
    from apps.accounts.models import Permission, Role, User
    perm, _ = Permission.objects.get_or_create(code="students.manage", defaults={"name": "x", "module": "students"})
    role, _ = Role.objects.get_or_create(name="academy_admin")
    role.permissions.add(perm)
    u = User(username="t1", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    r = client.post("/api/auth/login/", {"username": "t1", "password": "pass12345"})
    assert r.status_code == 200, r.content
    assert "access" in r.data


@pytest.mark.django_db
def test_duplicate_attendance_prevented(client):
    from datetime import date, time
    from apps.academy.models import Sport, TrainingLocation
    from apps.accounts.models import Role, User
    from apps.attendance.models import AttendanceRecord
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.sessions.models import Session, SessionStudent
    from apps.students.models import Student
    sport = Sport.objects.create(name="T-Football", code="TFB")
    coach = Coach.objects.create(trainer_id="T-1", first_name="C", phone="1", weekly_off_days=[5, 6])
    loc = TrainingLocation.objects.create(name="T-Loc")
    batch = Batch.objects.create(name="T-B", sport=sport, coach=coach, start_time=time(7, 0), end_time=time(8, 0), location=loc)
    st = Student.objects.create(student_id="T-S1", first_name="S", enrollment_date=date.today(), sport=sport)
    sess = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date(2026, 10, 14),
                                  start_time=time(7, 0), end_time=time(8, 0), location=loc)
    SessionStudent.objects.create(session=sess, student=st)
    AttendanceRecord.objects.create(session=sess, student=st, status="present")
    from django.db import IntegrityError
    with pytest.raises(IntegrityError):
        AttendanceRecord.objects.create(session=sess, student=st, status="absent")


@pytest.mark.django_db
def test_payment_webhook_requires_signature(client):
    from datetime import date, time
    from apps.academy.models import Sport
    from apps.fees.models import FeePlan, StudentFee
    from apps.payments.models import Payment
    from apps.students.models import Student
    sport = Sport.objects.create(name="P-Football", code="PFB")
    st = Student.objects.create(student_id="P-S1", first_name="S", enrollment_date=date.today(), sport=sport)
    plan = FeePlan.objects.create(name="P-Plan", cycle="monthly", amount=1000)
    fee = StudentFee.objects.create(student=st, fee_plan=plan, period_label="2026-10", amount=1000, due_date=date.today())
    pay = Payment.objects.create(student_fee=fee, amount=1000, provider="mock",
                                 provider_order_id="mock_order_x", idempotency_key="k1", status="pending")
    r = client.post("/api/payments/webhook/", {"order_id": "mock_order_x", "payment_id": "p1", "signature": "bad"}, format="json")
    assert r.status_code == 400
    pay.refresh_from_db()
    assert pay.status == "failed"


@pytest.mark.django_db
def test_payment_webhook_valid_signature_marks_success(client, monkeypatch):
    import hashlib
    import hmac
    from datetime import date

    from apps.academy.models import Sport
    from apps.fees.models import FeePlan, StudentFee
    from apps.payments.models import Payment
    from apps.students.models import Student
    monkeypatch.setenv("MOCK_WEBHOOK_SECRET", "s3cret")
    sport = Sport.objects.create(name="PW-Football", code="PWF")
    st = Student.objects.create(student_id="PW-S1", first_name="S", enrollment_date=date.today(), sport=sport)
    plan = FeePlan.objects.create(name="PW-Plan", cycle="monthly", amount=1000)
    fee = StudentFee.objects.create(student=st, fee_plan=plan, period_label="2026-10", amount=1000, due_date=date.today())
    pay = Payment.objects.create(student_fee=fee, amount=1000, provider="mock",
                                 provider_order_id="mock_order_y", idempotency_key="k2", status="pending")
    sig = hmac.new(b"s3cret", b"mock_order_yp1", hashlib.sha256).hexdigest()
    r = client.post("/api/payments/webhook/",
                    {"order_id": "mock_order_y", "payment_id": "p1", "signature": sig}, format="json")
    assert r.status_code == 200, r.content
    pay.refresh_from_db()
    assert pay.status == "success"
    assert pay.verified is True


def test_mock_webhook_fails_closed_without_secret(monkeypatch):
    from apps.payments.gateway import MockGateway
    monkeypatch.delenv("MOCK_WEBHOOK_SECRET", raising=False)
    assert MockGateway().verify_webhook(payload={"order_id": "o", "payment_id": "p"},
                                        signature="whatever") is False


def test_stripe_webhook_requires_real_signature(monkeypatch):
    import hashlib
    import hmac
    import time

    from apps.payments.gateway import StripeGateway
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    g = StripeGateway()
    body = b'{"order_id":"o1","payment_id":"p1"}'
    ts = str(int(time.time()))
    good = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert g.verify_webhook(payload={}, signature=f"t={ts},v1={good}", raw_body=body) is True
    # Regression: any non-empty signature used to pass; now it must not.
    assert g.verify_webhook(payload={}, signature="anything", raw_body=body) is False
    assert g.verify_webhook(payload={}, signature=f"t={ts},v1=deadbeef", raw_body=body) is False
    # Missing secret fails closed.
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    assert g.verify_webhook(payload={}, signature=f"t={ts},v1={good}", raw_body=body) is False


@pytest.mark.django_db
def test_trainer_fee_only_completed():
    from datetime import date, time
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.sessions.models import Session
    from apps.trainer_fees.engine import compute_payables_for_session
    from apps.trainer_fees.models import TrainerFeeRule
    sport = Sport.objects.create(name="F-Football", code="FFB")
    coach = Coach.objects.create(trainer_id="F-1", first_name="C", phone="2", weekly_off_days=[5, 6])
    loc = TrainingLocation.objects.create(name="F-Loc")
    batch = Batch.objects.create(name="F-B", sport=sport, coach=coach, start_time=time(7, 0), end_time=time(8, 0), location=loc)
    # Dates are relative to today so the rule is always effective on the
    # session date (a hardcoded past date would fall outside the rule window).
    TrainerFeeRule.objects.create(trainer=coach, rate_per_session=500, effective_from=date.today())
    sched = Session.objects.create(sport=sport, batch=batch, coach=coach, date=date.today(),
                                   start_time=time(7, 0), end_time=time(8, 0), status="scheduled")
    assert compute_payables_for_session(sched) is None
    sched.status = "completed"
    sched.save()
    assert compute_payables_for_session(sched) is not None
