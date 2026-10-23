"""Parent mobile app backend: linked-child isolation, payment receipts,
self-service profile, and notification scoping."""
from datetime import date
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from apps.academy.models import School
from apps.accounts.models import Permission, Role, User
from apps.fees.models import FeePlan, StudentFee
from apps.notifications.models import Notification
from apps.parents.models import Parent, StudentParent
from apps.payments.models import Payment
from apps.students.models import Student

PARENT_CODES = ["students.view", "attendance.view", "fees.view", "sessions.view",
                "notifications.view", "payments.view",
                "support.view", "support.create", "support.reply"]


def _role(name, codes):
    role, _ = Role.objects.get_or_create(name=name)
    role.permissions.set(Permission.objects.filter(code__in=codes))
    return role


def _user(username, role, **kw):
    u = User(username=username, role=role, is_active=True, **kw)
    u.set_password("pass12345")
    u.save()
    return u


def _login(username, pwd="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": username, "password": pwd})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


def _setup():
    school = School.objects.create(name="Parent U Academy", code="PU1")
    parent_role = _role("parent", PARENT_CODES)

    own = Student.objects.create(student_id="PU-S1", first_name="Aarav", school=school,
                                 enrollment_date=date.today())
    other = Student.objects.create(student_id="PU-S2", first_name="Bela", school=school,
                                   enrollment_date=date.today())

    parent = Parent.objects.create(first_name="Ravi", last_name="Kumar", relationship="father",
                                   mobile="9000000111", email="ravi@example.com")
    StudentParent.objects.create(student=own, parent=parent, is_primary=True)
    other_parent = Parent.objects.create(first_name="Other", relationship="guardian",
                                         mobile="9000000222")

    _user("parent1", parent_role, parent_profile=parent)
    _user("parent2", parent_role, parent_profile=other_parent)
    # A parent-role account with no guardian record linked.
    _user("noparent", parent_role)

    plan = FeePlan.objects.create(name="Monthly", cycle="monthly", amount=Decimal("1000"))
    sf_own = StudentFee.objects.create(student=own, fee_plan=plan, period_label="2026-10",
                                       amount=Decimal("1000"), due_date=date.today(), status="due")
    sf_other = StudentFee.objects.create(student=other, fee_plan=plan, period_label="2026-10",
                                         amount=Decimal("1000"), due_date=date.today(), status="due")

    Payment.objects.create(student_fee=sf_own, amount=Decimal("1000"), status="success",
                           verified=True, idempotency_key="pu-own")
    Payment.objects.create(student_fee=sf_other, amount=Decimal("1000"), status="success",
                           verified=True, idempotency_key="pu-other")

    Notification.objects.create(user=User.objects.get(username="parent1"), title="For me",
                                message="own notice")
    Notification.objects.create(user=User.objects.get(username="parent2"), title="For other",
                                message="other notice")
    return own, other


@pytest.mark.django_db
def test_parent_sees_only_linked_students():
    own, other = _setup()
    cl = _login("parent1")
    r = cl.get("/api/students/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    ids = {row["id"] for row in rows}
    assert ids == {own.id}
    assert other.id not in ids


@pytest.mark.django_db
def test_parent_fees_are_scoped_to_linked_students():
    own, other = _setup()
    cl = _login("parent1")
    r = cl.get("/api/fees/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert {row["student"] for row in rows} == {own.id}
    # Asking explicitly for another family's student returns nothing.
    r = cl.get(f"/api/fees/?student={other.id}")
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert rows == []


@pytest.mark.django_db
def test_parent_sees_only_own_payment_receipts():
    own, other = _setup()
    cl = _login("parent1")
    r = cl.get("/api/payments/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert len(rows) == 1
    assert rows[0]["student_fee"]  # receipt present, scoped to own child


@pytest.mark.django_db
def test_parent_attendance_scoped_to_linked_students():
    own, other = _setup()
    cl = _login("parent1")
    r = cl.get("/api/attendance/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert all(row["student"] == own.id for row in rows)


@pytest.mark.django_db
def test_parent_sees_only_own_notifications():
    _setup()
    cl = _login("parent1")
    r = cl.get("/api/notifications/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert [row["title"] for row in rows] == ["For me"]


@pytest.mark.django_db
def test_parent_me_get_and_patch_contact_fields():
    own, other = _setup()
    cl = _login("parent1")
    r = cl.get("/api/parents/me/")
    assert r.status_code == 200, r.content
    assert r.data["first_name"] == "Ravi"
    assert r.data["mobile"] == "9000000111"

    r = cl.patch("/api/parents/me/", {"mobile": "9000000999",
                                      "communication_preference": "email"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["mobile"] == "9000000999"
    assert r.data["communication_preference"] == "email"
    # Administrative fields are read-only: relationship cannot be self-edited.
    r = cl.patch("/api/parents/me/", {"relationship": "mother"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["relationship"] == "father"


@pytest.mark.django_db
def test_parent_me_404_without_linked_profile():
    _setup()
    cl = _login("noparent")
    r = cl.get("/api/parents/me/")
    assert r.status_code == 404, r.content


@pytest.mark.django_db
def test_parent_support_tickets_are_raiser_scoped():
    _setup()
    cl = _login("parent1")
    r = cl.post("/api/support/tickets/", {"subject": "Fee receipt", "category": "billing",
                                          "priority": "medium",
                                          "description": "Please share October receipt"},
                format="json")
    assert r.status_code == 201, r.content
    ticket_id = r.data["id"]

    # A different parent cannot see this ticket.
    cl2 = _login("parent2")
    r = cl2.get("/api/support/tickets/")
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert rows == []

    r = cl.get(f"/api/support/tickets/{ticket_id}/")
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_parent_cannot_browse_guardian_directory():
    # The guardian directory stays closed; the app uses /parents/me/ for
    # self-service instead, so a parent holds no parents.view grant.
    _setup()
    cl = _login("parent1")
    r = cl.get("/api/parents/")
    assert r.status_code == 403, r.content
