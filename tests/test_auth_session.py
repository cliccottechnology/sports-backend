"""Role sessions: absolute session cap, no silent extension, parent isolation.

Lifetimes are settings-driven; these tests use short-circuit paths (crafted
claims, blacklist, scoping) so nothing waits hours. The absolute cap is
``settings.JWT_SESSION_MINUTES`` (default 12h).
"""
import time

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def password_client():
    from apps.accounts.models import User
    u = User(username="sess_user", is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "sess_user", "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl, r.data


@pytest.mark.django_db
def test_login_issues_session_deadline(password_client):
    import time as _t
    from django.conf import settings
    cl, data = password_client
    minutes = int(getattr(settings, "JWT_SESSION_MINUTES", 720))
    assert "session_exp" in data and data["session_exp"]
    assert (minutes - 1) * 60 < data["session_exp"] - int(_t.time()) <= minutes * 60
    assert cl.get("/api/auth/me/").status_code == 200


@pytest.mark.django_db
def test_expired_session_rejected_everywhere(password_client):
    import jwt
    from django.conf import settings
    cl, data = password_client
    # Forge the same shape with a past deadline (valid signature path).
    from apps.accounts.auth import SessionRefreshToken
    from apps.accounts.models import User
    user = User.objects.get(username="sess_user")
    tok = SessionRefreshToken.for_user(user)
    tok["session_exp"] = int(time.time()) - 10
    access = str(tok.access_token)
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    assert cl.get("/api/auth/me/").status_code == 401


@pytest.mark.django_db
def test_refresh_cannot_extend_past_deadline(password_client):
    from apps.accounts.auth import SessionRefreshToken
    from apps.accounts.models import User
    user = User.objects.get(username="sess_user")
    tok = SessionRefreshToken.for_user(user)
    tok["session_exp"] = int(time.time()) - 10
    cl = APIClient()
    r = cl.post("/api/auth/refresh/", {"refresh": str(tok)})
    assert r.status_code == 401, r.content


@pytest.mark.django_db
def test_refresh_without_deadline_rejected(password_client):
    from rest_framework_simplejwt.tokens import RefreshToken
    from apps.accounts.models import User
    user = User.objects.get(username="sess_user")
    old = RefreshToken.for_user(user)  # pre-rollout shape: no session_exp
    assert "session_exp" not in old
    cl = APIClient()
    assert cl.post("/api/auth/refresh/", {"refresh": str(old)}).status_code == 401


@pytest.mark.django_db
def test_logout_blacklists_refresh(password_client):
    cl, data = password_client
    refresh = data["refresh"]
    assert cl.post("/api/auth/logout/", {"refresh": refresh}).status_code == 200
    assert APIClient().post("/api/auth/refresh/", {"refresh": refresh}).status_code == 401


@pytest.mark.django_db
def test_parent_sees_only_linked_students():
    from datetime import date
    from apps.accounts.models import Role, User
    from apps.parents.models import Parent, StudentParent
    from apps.students.models import Student
    mine = Student.objects.create(student_id="P-KID", first_name="Kid", enrollment_date=date.today())
    other = Student.objects.create(student_id="P-STRANGER", first_name="Stranger", enrollment_date=date.today())
    parent = Parent.objects.create(first_name="Mom", relationship="mother", mobile="9000000009")
    StudentParent.objects.create(student=mine, parent=parent, is_primary=True)
    role, _ = Role.objects.get_or_create(name="parent")
    from apps.accounts.models import Permission
    role.permissions.set(Permission.objects.filter(
        code__in=["students.view", "attendance.view", "fees.view", "sessions.view"]))
    u = User(username="mom1", role=role, is_active=True, parent_profile=parent)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "mom1", "password": "pass12345"})
    assert r.data["user"]["role_name"] == "parent"
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    rows = cl.get("/api/students/").data
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert [s["student_id"] for s in rows] == ["P-KID"]
    # direct object access to the stranger is also blocked
    assert cl.get(f"/api/students/{other.id}/").status_code == 404
    fees = cl.get("/api/fees/", {"student": other.id}).data
    fees = fees["results"] if isinstance(fees, dict) else fees
    assert fees == []
    att = cl.get("/api/attendance/", {"student": other.id}).data
    att = att["results"] if isinstance(att, dict) else att
    assert att == []
    # ...while own child's rows flow normally
    assert cl.get("/api/fees/", {"student": mine.id}).status_code == 200


@pytest.mark.django_db
def test_parent_cannot_write(password_client):
    from apps.accounts.models import Role, User
    role, _ = Role.objects.get_or_create(name="parent")
    u = User(username="mom2", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "mom2", "password": "pass12345"})
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    assert cl.post("/api/students/", {"student_id": "X", "first_name": "X"}, format="json").status_code == 403
