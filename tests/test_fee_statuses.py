"""Fee status options: offline payments, overdue marking, summary."""
from datetime import date, timedelta

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def setup():
    from apps.academy.models import Sport
    from apps.fees.models import FeePlan, StudentFee
    from apps.students.models import Student
    sport = Sport.objects.create(name="F-Football", code="FFB")
    st = Student.objects.create(student_id="F-S1", first_name="Fee", enrollment_date=date.today(), sport=sport)
    plan = FeePlan.objects.create(name="F-Plan", cycle="monthly", amount=2000)
    fee = StudentFee.objects.create(student=st, fee_plan=plan, period_label="2026-10",
                                    amount=2000, due_date=date.today() + timedelta(days=5))
    return {"fee": fee, "student": st}


@pytest.fixture
def staff():
    from apps.accounts.models import Permission, Role, User
    perms = []
    for code in ("fees.manage", "payments.verify"):
        p, _ = Permission.objects.get_or_create(code=code, defaults={"name": code, "module": code.split(".")[0]})
        perms.append(p)
    role, _ = Role.objects.get_or_create(name="fee_staff")
    role.permissions.add(*perms)
    u = User(username="fee_staff", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.mark.django_db
def test_offline_payment_full_marks_paid(staff, setup):
    fee = setup["fee"]
    r = staff.post(f"/api/fees/{fee.pk}/record_payment/",
                   {"amount": 2000, "method": "cash", "reference": "C-1"}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["fee_status"] == "paid"
    assert r.data["balance"] == 0
    assert r.data["receipt"].startswith("OFF-")
    fee.refresh_from_db()
    assert fee.status == "paid"
    assert fee.payments.filter(status="success", verified=True).count() == 1


@pytest.mark.django_db
def test_offline_payment_partial(staff, setup):
    fee = setup["fee"]
    r = staff.post(f"/api/fees/{fee.pk}/record_payment/",
                   {"amount": 500, "method": "upi"}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["fee_status"] == "partially_paid"
    assert r.data["balance"] == 1500


@pytest.mark.django_db
def test_offline_payment_rejects_bad(staff, setup):
    fee = setup["fee"]
    assert staff.post(f"/api/fees/{fee.pk}/record_payment/", {"amount": 0}, format="json").status_code == 400
    assert staff.post(f"/api/fees/{fee.pk}/record_payment/", {"amount": 9999}, format="json").status_code == 400
    assert staff.post(f"/api/fees/{fee.pk}/record_payment/", {"amount": 100, "method": "card"}, format="json").status_code == 400
    fee.status = "cancelled"
    fee.save()
    assert staff.post(f"/api/fees/{fee.pk}/record_payment/", {"amount": 100}, format="json").status_code == 400


@pytest.mark.django_db
def test_offline_payment_needs_perm(setup):
    from apps.accounts.models import User
    u = User(username="noperm", is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    r = cl.post(f"/api/fees/{setup['fee'].pk}/record_payment/", {"amount": 100}, format="json")
    assert r.status_code == 403


@pytest.mark.django_db
def test_mark_overdue(staff, setup):
    from apps.fees.models import StudentFee
    fee = setup["fee"]
    fee.due_date = date.today() - timedelta(days=3)
    fee.save()
    paid_fee = StudentFee.objects.create(student=setup["student"], fee_plan=fee.fee_plan,
                                         period_label="2026-09", amount=1000,
                                         due_date=date.today() - timedelta(days=10), status="paid")
    r = staff.post("/api/fees/mark_overdue/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["marked_overdue"] == 1
    fee.refresh_from_db()
    assert fee.status == "overdue"
    paid_fee.refresh_from_db()
    assert paid_fee.status == "paid"


@pytest.mark.django_db
def test_fee_summary(staff, setup):
    r = staff.get("/api/fees/summary/")
    assert r.status_code == 200, r.content
    assert r.data["billed"] == 2000
    assert r.data["outstanding"] == 2000
    assert any(b["status"] == "due" and b["count"] == 1 for b in r.data["by_status"])
