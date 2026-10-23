"""Support tickets: raiser isolation, manager visibility, replies, internal notes."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Permission, Role, User


def _mkuser(username, role=None, pwd="pass12345"):
    u = User(username=username, role=role, is_active=True)
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


@pytest.mark.django_db
def test_raiser_sees_only_own_tickets():
    raiser = _mkuser("sup_raiser", _role("sup_raiser_role", ["support.view", "support.create"]))
    other = _mkuser("sup_other", _role("sup_other_role", ["support.view", "support.create"]))
    cl = _login("sup_raiser")
    r = cl.post("/api/support/tickets/", {"subject": "Cannot save", "description": "error on save",
                                          "category": "technical", "priority": "high"}, format="json")
    assert r.status_code == 201, r.content
    tid = r.data["id"]
    # Raiser sees own ticket
    assert cl.get("/api/support/tickets/").data["count"] == 1
    # Another raiser cannot see it nor fetch it by id
    assert _login("sup_other").get("/api/support/tickets/").data["count"] == 0
    assert _login("sup_other").get(f"/api/support/tickets/{tid}/").status_code == 404


@pytest.mark.django_db
def test_manager_sees_all_and_can_manage():
    raiser = _mkuser("sup_r2", _role("sup_r2_role", ["support.view", "support.create"]))
    _mkuser("sup_mgr", _role("sup_mgr_role", ["support.view", "support.reply", "support.manage"]))
    rcl = _login("sup_r2")
    r = rcl.post("/api/support/tickets/", {"subject": "Fee bug", "description": "wrong total"}, format="json")
    assert r.status_code == 201, r.content
    tid = r.data["id"]

    mcl = _login("sup_mgr")
    assert mcl.get("/api/support/tickets/").data["count"] == 1
    # Manager reply + internal note
    assert mcl.post(f"/api/support/tickets/{tid}/reply/", {"body": "Looking into it"}, format="json").status_code == 201
    assert mcl.post(f"/api/support/tickets/{tid}/reply/", {"body": "internal", "is_internal": True}, format="json").status_code == 201
    # Assign + status change
    res = mcl.post(f"/api/support/tickets/{tid}/assign/", {"status": "in_progress", "assigned_to": None}, format="json")
    assert res.status_code == 200 and res.data["status"] == "in_progress"
    # Raiser sees the public reply but NOT the internal note
    detail = rcl.get(f"/api/support/tickets/{tid}/").data
    assert len(detail["replies"]) == 1
    assert detail["replies"][0]["body"] == "Looking into it"


@pytest.mark.django_db
def test_no_permission_blocked():
    _mkuser("sup_none", _role("sup_none_role", []))
    cl = _login("sup_none")
    assert cl.get("/api/support/tickets/").status_code == 403
    assert cl.post("/api/support/tickets/", {"subject": "x", "description": "y"}, format="json").status_code == 403


@pytest.mark.django_db
def test_close_sets_resolved_at_and_reply_reopens():
    raiser = _mkuser("sup_r3", _role("sup_r3_role", ["support.view", "support.create", "support.reply"]))
    _mkuser("sup_mgr2", _role("sup_mgr2_role", ["support.view", "support.reply", "support.manage"]))
    rcl = _login("sup_r3")
    tid = rcl.post("/api/support/tickets/", {"subject": "S", "description": "D"}, format="json").data["id"]
    mcl = _login("sup_mgr2")
    res = mcl.post(f"/api/support/tickets/{tid}/assign/", {"status": "resolved"}, format="json")
    assert res.data["status"] == "resolved" and res.data["resolved_at"]
    # Raiser replies -> reopens
    r = rcl.post(f"/api/support/tickets/{tid}/reply/", {"body": "still broken"}, format="json")
    assert r.data["status"] == "open" and r.data["resolved_at"] is None
