"""Customization request workflow: submission, review, transitions, isolation,
permissions, notifications and audit trail."""
import pytest
from rest_framework.test import APIClient

from apps.academy.models import School
from apps.accounts.models import Permission, Role, User
from apps.audit.models import AuditLog
from apps.customization.models import CustomizationRequest
from apps.notifications.models import Notification

BASE = "/api/customization/requests/"


def _mkuser(username, role=None, school=None, pwd="pass12345"):
    u = User(username=username, role=role, school=school, is_active=True)
    u.set_password(pwd)
    u.save()
    return u


def _login(username, pwd="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": username, "password": pwd})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


def _role(name, codes):
    role, _ = Role.objects.get_or_create(name=name)
    role.permissions.set(Permission.objects.filter(code__in=codes))
    return role


ADMIN_CODES = ["customization.view", "customization.create", "customization.reply"]
REVIEW_CODES = ADMIN_CODES + ["customization.review", "customization.manage"]


def _setup():
    school = School.objects.create(name="Alpha Academy", code="ALPHA")
    other = School.objects.create(name="Beta Academy", code="BETA")
    admin = _mkuser("cr_admin", _role("cr_admin_role", ADMIN_CODES), school=school)
    sa = _mkuser("cr_sa", _role("cr_sa_role", REVIEW_CODES))  # super-admin-equivalent reviewer
    other_admin = _mkuser("cr_other", _role("cr_other_role", ADMIN_CODES), school=other)
    return school, other, admin, sa, other_admin


def _submit(cl, title="Customize Attendance Report"):
    r = cl.post(BASE, {"title": title, "category": "report",
                       "description": "Need an extra attendance report",
                       "reason": "Management reporting", "priority": "high",
                       "expected_outcome": "New report page"}, format="json")
    assert r.status_code == 201, r.content
    return r.data


@pytest.mark.django_db
def test_admin_submits_and_gets_request_id_and_sa_sees_it():
    _setup()
    acl = _login("cr_admin")
    data = _submit(acl)
    assert data["request_id"].startswith("HARI-CR-")
    assert data["status"] == "submitted"
    assert data["created_by_name"] == "cr_admin"

    scl = _login("cr_sa")
    listing = scl.get(BASE).data
    assert listing["count"] == 1
    # Super Admin notified of the new request.
    assert Notification.objects.filter(user__username="cr_sa").count() >= 1


@pytest.mark.django_db
def test_admin_cannot_approve_own_request():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    r = acl.post(f"{BASE}{rid}/transition/", {"action": "approve"}, format="json")
    assert r.status_code == 403
    # Admin cannot see review-only actions succeed; request stays submitted.
    assert CustomizationRequest.objects.get(pk=rid).status == "submitted"


@pytest.mark.django_db
def test_full_workflow_and_status_transitions():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    scl = _login("cr_sa")

    def act(name, **extra):
        payload = {"action": name, **extra}
        return scl.post(f"{BASE}{rid}/transition/", payload, format="json")

    assert act("review").data["status"] == "under_review"
    # Clarification round-trip
    assert act("clarify", note="Which report exactly?").data["status"] == "clarification_required"
    assert acl.post(f"{BASE}{rid}/reply/", {"body": "The monthly summary"}, format="json").data["status"] == "under_review"
    # Approve -> assign -> start -> ready -> complete
    assert act("approve").data["status"] == "approved"
    assert act("set_priority", priority="urgent").data["priority"] == "urgent"
    assert act("assign", assigned_team="Dev Team A").data["status"] == "assigned"
    assert act("start").data["status"] == "in_progress"
    assert act("ready").data["status"] == "ready_for_review"
    done = act("complete").data
    assert done["status"] == "completed" and done["completed_at"]
    # Reopen completed work
    assert act("reopen").data["status"] == "in_progress"

    # Every step is preserved in the status history.
    req = CustomizationRequest.objects.get(pk=rid)
    assert req.history.count() >= 9


@pytest.mark.django_db
def test_invalid_transition_rejected():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    scl = _login("cr_sa")
    # Cannot jump straight from Submitted to Completed.
    r = scl.post(f"{BASE}{rid}/transition/", {"action": "complete"}, format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_reject_requires_reason_and_admin_sees_it():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    scl = _login("cr_sa")
    assert scl.post(f"{BASE}{rid}/transition/", {"action": "reject"}, format="json").status_code == 400
    r = scl.post(f"{BASE}{rid}/transition/", {"action": "reject", "reason": "Out of scope"}, format="json")
    assert r.status_code == 200 and r.data["status"] == "rejected"
    detail = acl.get(f"{BASE}{rid}/").data
    assert detail["decision_reason"] == "Out of scope"


@pytest.mark.django_db
def test_school_isolation():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    ocl = _login("cr_other")
    assert ocl.get(BASE).data["count"] == 0
    assert ocl.get(f"{BASE}{rid}/").status_code == 404


@pytest.mark.django_db
def test_no_permission_blocked():
    _mkuser("cr_none", _role("cr_none_role", []))
    cl = _login("cr_none")
    assert cl.get(BASE).status_code == 403
    assert cl.post(BASE, {"title": "x", "description": "y"}, format="json").status_code == 403


@pytest.mark.django_db
def test_assign_notifies_assignee_and_audit_records_actions():
    _setup()
    dev = _mkuser("cr_dev", _role("cr_dev_role", ADMIN_CODES))
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    scl = _login("cr_sa")
    scl.post(f"{BASE}{rid}/transition/", {"action": "approve"}, format="json")
    r = scl.post(f"{BASE}{rid}/transition/",
                 {"action": "assign", "assigned_to": dev.pk}, format="json")
    assert r.status_code == 200 and r.data["assigned_to"] == dev.pk
    assert Notification.objects.filter(user=dev).exists()
    assert AuditLog.objects.filter(module="customization").count() >= 3


@pytest.mark.django_db
def test_internal_notes_hidden_from_requester():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    scl = _login("cr_sa")
    scl.post(f"{BASE}{rid}/reply/", {"body": "internal only", "is_internal": True}, format="json")
    scl.post(f"{BASE}{rid}/reply/", {"body": "public note"}, format="json")
    detail = acl.get(f"{BASE}{rid}/").data
    bodies = [c["body"] for c in detail["comments"]]
    assert "public note" in bodies and "internal only" not in bodies


@pytest.mark.django_db
def test_direct_status_patch_is_ignored():
    _setup()
    acl = _login("cr_admin")
    rid = _submit(acl)["id"]
    # Admin tries to self-approve by PATCHing status directly.
    r = acl.patch(f"{BASE}{rid}/", {"status": "approved", "assigned_to": None}, format="json")
    assert r.status_code == 200
    assert CustomizationRequest.objects.get(pk=rid).status == "submitted"


@pytest.mark.django_db
def test_stats_reflect_database():
    _setup()
    acl = _login("cr_admin")
    _submit(acl, title="One")
    _submit(acl, title="Two")
    scl = _login("cr_sa")
    stats = scl.get(f"{BASE}stats/").data
    assert stats["total"] == 2 and stats["submitted"] == 2
