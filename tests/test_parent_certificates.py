"""Parent App: certificate eligibility, certificate visibility and fee/payment
access — all row-locked to the authenticated parent's linked children.

Exercises the real endpoints the mobile app calls:
  GET  /api/students/certificates/eligibility/
  GET  /api/students/certificates/
  POST /api/students/certificates/issue/
  GET  /api/payments/
  POST /api/payments/
"""
from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Permission, Role, User
from apps.fees.models import FeePlan, StudentFee
from apps.parents.models import Parent, StudentParent
from apps.payments.models import Payment
from apps.students.models import Student

PARENT_CODES = ["students.view", "attendance.view", "fees.view", "sessions.view",
                "notifications.view", "payments.view",
                "support.view", "support.create", "support.reply"]


def _role(name, codes=()):
    role, _ = Role.objects.get_or_create(name=name)
    if codes:
        role.permissions.set(Permission.objects.filter(code__in=codes))
    return role


def _setup_family():
    """Two unrelated families, each with one child enrolled today."""
    parent_role = _role("parent", PARENT_CODES)
    _role("coach")
    _role("academy_admin")

    # Family A — our parent.
    pa = Parent.objects.create(first_name="Ravi", relationship="father", mobile="9000000111")
    ka = Student.objects.create(student_id="PC-A", first_name="Aarav", enrollment_date=date.today())
    StudentParent.objects.create(student=ka, parent=pa, is_primary=True)
    User.objects.create_user(username="p_a", password="pass12345", role=parent_role,
                             parent_profile=pa, phone="9000000111")

    # Family B — a different parent we must never leak to.
    pb = Parent.objects.create(first_name="Other", relationship="guardian", mobile="9000000222")
    kb = Student.objects.create(student_id="PC-B", first_name="Bela", enrollment_date=date.today())
    StudentParent.objects.create(student=kb, parent=pb, is_primary=True)
    User.objects.create_user(username="p_b", password="pass12345", role=parent_role,
                             parent_profile=pb, phone="9000000222")
    return ka, kb


def _fee(student, amount="1000.00"):
    plan = FeePlan.objects.create(name=f"Monthly-{student.student_id}", cycle="monthly",
                                  amount=Decimal(amount))
    return StudentFee.objects.create(student=student, fee_plan=plan, period_label="2026-10",
                                     amount=Decimal(amount), due_date=date.today())


def _client(username, password="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/parent/login/", {"username": username, "password": password})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


@pytest.mark.django_db
def test_eligibility_is_scoped_to_the_parents_own_children():
    ka, kb = _setup_family()
    cl = _client("p_a")

    r = cl.get("/api/students/certificates/eligibility/")
    assert r.status_code == 200, r.content
    assert "policy" in r.data and "results" in r.data
    ids = {row["student"] for row in r.data["results"]}
    assert ids == {ka.id}
    assert kb.id not in ids

    row = next(row for row in r.data["results"] if row["student"] == ka.id)
    # A child enrolled today sits well before the 3-month milestone.
    assert row["state"] == "training"
    assert set(row["checks"]) == {"active", "attendance", "sessions"}
    assert row["milestone_date"]  # computed from the real enrolment date


@pytest.mark.django_db
def test_milestone_may_be_eligible_but_is_never_auto_issued():
    _setup_family()
    cl = _client("p_a")
    row = cl.get("/api/students/certificates/eligibility/").data["results"][0]
    # Eligibility is surfaced to the parent, but no certificate exists yet —
    # issuance is an explicit academy action, not an automatic side effect.
    assert row["state"] != "issued"
    assert row["certificate"] is None


@pytest.mark.django_db
def test_parent_sees_only_own_certificates_and_cannot_issue():
    ka, kb = _setup_family()
    cl = _client("p_a")

    # No certificates exist yet; the scoped list must not leak family B.
    r = cl.get("/api/students/certificates/", {"student": kb.id})
    rows = r.data.get("results", r.data) if isinstance(r.data, dict) else r.data
    assert r.status_code == 200 and rows == []

    # Issuance requires students.edit, which a parent never has.
    denied = cl.post("/api/students/certificates/issue/", {"student": ka.id}, format="json")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_parent_payment_access_is_read_only_and_row_locked():
    ka, kb = _setup_family()
    fee_a = _fee(ka)
    fee_b = _fee(kb, "2000.00")
    Payment.objects.create(student_fee=fee_a, amount=Decimal("1000.00"), status="success",
                           verified=True, receipt_no="RCP-A-1", idempotency_key="idem-a")
    Payment.objects.create(student_fee=fee_b, amount=Decimal("2000.00"), status="success",
                           verified=True, receipt_no="RCP-B-1", idempotency_key="idem-b")

    cl = _client("p_a")
    r = cl.get("/api/payments/")
    assert r.status_code == 200, r.content
    rows = r.data.get("results", r.data) if isinstance(r.data, dict) else r.data
    assert {p["student_fee"] for p in rows} == {fee_a.id}
    assert all(p["receipt_no"] == "RCP-A-1" for p in rows)

    # Receipts are view-only for parents — they cannot initiate a payment.
    denied = cl.post("/api/payments/", {"student_fee": fee_a.id, "amount": "100"},
                     format="json")
    assert denied.status_code == 403
