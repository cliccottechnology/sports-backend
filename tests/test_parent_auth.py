"""Separate Parent App authentication and token.

Covers the acceptance criteria: independent parent/teacher logins, distinct
token identities, cross-role API denial, parent-only endpoint lockdown,
server-enforced absolute session expiry, refresh, blacklist-based logout isolation,
and that the existing shared login keeps working unchanged.
"""
import time

import jwt
import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.accounts.models import Permission, Role, User
from apps.parents.models import Parent, StudentParent
from apps.students.models import Student
from datetime import date

PARENT_CODES = ["students.view", "attendance.view", "fees.view", "sessions.view",
                "notifications.view", "payments.view",
                "support.view", "support.create", "support.reply"]


def _role(name, codes=()):
    role, _ = Role.objects.get_or_create(name=name)
    if codes:
        role.permissions.set(Permission.objects.filter(code__in=codes))
    return role


def _user(username, role, **kw):
    u = User(username=username, role=role, is_active=True, **kw)
    u.set_password("pass12345")
    u.save()
    return u


def _decode(access):
    return jwt.decode(access, settings.SECRET_KEY, algorithms=["HS256"])


def _setup():
    parent_role = _role("parent", PARENT_CODES)
    coach_role = _role("coach")
    admin_role = _role("academy_admin")
    parent = Parent.objects.create(first_name="Ravi", relationship="father",
                                   mobile="9000000111", email="ravi@example.com")
    kid = Student.objects.create(student_id="PA-S1", first_name="Aarav",
                                 enrollment_date=date.today())
    StudentParent.objects.create(student=kid, parent=parent, is_primary=True)
    _user("p_phone", parent_role, parent_profile=parent, phone="9000000111",
          email="ravi@example.com")
    _user("p_other", parent_role, parent_profile=Parent.objects.create(
        first_name="Other", relationship="guardian", mobile="9000000222"))
    _user("c_coach", coach_role)
    _user("a_admin", admin_role)
    return kid


def _parent_login(identifier="p_phone", pwd="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/parent/login/", {"username": identifier, "password": pwd})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl, r.data


def _token_client(access):
    cl = APIClient()
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    return cl


# --- 1 & 2: independent logins, distinct identities -------------------------

@pytest.mark.django_db
def test_parent_and_teacher_login_independently_with_distinct_tokens():
    _setup()
    _, p = _parent_login()

    teacher = APIClient().post("/api/auth/trainer/login/",
                               {"username": "c_coach", "password": "pass12345"})
    assert teacher.status_code == 200, teacher.content

    # Parent token is labelled with its own portal and carries the guardian id.
    pc = _decode(p["access"])
    assert pc["portal"] == "parent" and pc["role"] == "parent"
    assert pc["parent_id"] is not None
    assert p["portal"] == "parent" and p["user"]["parent_id"] is not None
    assert p["user"]["role_name"] == "parent"

    # Teacher token is a different identity: no parent portal, no parent id.
    tc = _decode(teacher.data["access"])
    assert tc["portal"] == "trainer" and tc["role"] == "coach"
    assert "parent_id" not in tc
    assert p["access"] != teacher.data["access"]


@pytest.mark.django_db
def test_parent_login_accepts_phone_username_and_email():
    _setup()
    for ident in ("9000000111", "p_phone", "ravi@example.com"):
        r = APIClient().post("/api/auth/parent/login/",
                             {"username": ident, "password": "pass12345"})
        assert r.status_code == 200, (ident, r.content)


@pytest.mark.django_db
def test_parent_login_rejects_wrong_role_and_bad_credentials():
    _setup()
    # A coach/admin cannot authenticate through the parent portal.
    for ident in ("c_coach", "a_admin"):
        r = APIClient().post("/api/auth/parent/login/",
                             {"username": ident, "password": "pass12345"})
        assert r.status_code == 403, (ident, r.content)
    # Invalid credentials are a plain 401, never a token.
    r = APIClient().post("/api/auth/parent/login/",
                         {"username": "p_phone", "password": "wrong"})
    assert r.status_code == 401
    assert "access" not in r.data


# --- 3: parent tokens cannot reach teacher/admin APIs -----------------------

@pytest.mark.django_db
def test_parent_token_cannot_access_admin_or_teacher_apis():
    _setup()
    cl, _ = _parent_login()
    for url in ["/api/users/", "/api/users/roles/", "/api/coaches/",
                "/api/batches/", "/api/documents/", "/api/customization/requests/"]:
        assert cl.get(url).status_code == 403, url
    assert cl.post("/api/users/", {"username": "x", "password": "pass12345"},
                   format="json").status_code == 403


# --- 4: teacher/admin tokens cannot reach parent-only APIs ------------------

@pytest.mark.django_db
def test_teacher_and_admin_tokens_cannot_use_parent_only_endpoint():
    _setup()
    for endpoint, ident in (("/api/auth/trainer/login/", "c_coach"),
                            ("/api/auth/login/", "a_admin")):
        r = APIClient().post(endpoint, {"username": ident, "password": "pass12345"})
        assert r.status_code == 200, r.content
        cl = _token_client(r.data["access"])
        assert cl.get("/api/parents/me/").status_code == 403, ident
    # A parent is allowed.
    cl, _ = _parent_login()
    assert cl.get("/api/parents/me/").status_code == 200


# --- 5: cannot read another family's child ----------------------------------

@pytest.mark.django_db
def test_parent_cannot_read_another_families_child_by_id():
    kid = _setup()
    other = Student.objects.create(student_id="PA-S2", first_name="Bela",
                                   enrollment_date=date.today())
    cl, _ = _parent_login()
    rows = cl.get("/api/students/").data
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert {r["id"] for r in rows} == {kid.id}
    assert cl.get(f"/api/students/{other.id}/").status_code == 404


# --- 6: server-enforced absolute session expiry -----------------------------

@pytest.mark.django_db
def test_parent_session_expiry_enforced_server_side():
    _setup()
    cl, _ = _parent_login()
    assert cl.get("/api/auth/me/").status_code == 200

    from apps.accounts.auth import SessionRefreshToken
    user = User.objects.get(username="p_phone")
    tok = SessionRefreshToken.for_user(user)
    tok["session_exp"] = int(time.time()) - 10  # expired a moment ago
    tok["role"] = "parent"
    tok["portal"] = "parent"
    expired = _token_client(str(tok.access_token))
    assert expired.get("/api/auth/me/").status_code == 401
    assert expired.get("/api/students/").status_code == 401


# --- 9: refresh + logout + revocation ---------------------------------------

@pytest.mark.django_db
def test_parent_refresh_preserves_identity_and_cannot_extend_past_deadline():
    _setup()
    _, data = _parent_login()
    r = APIClient().post("/api/auth/refresh/", {"refresh": data["refresh"]})
    assert r.status_code == 200, r.content
    claims = _decode(r.data["access"])
    assert claims["portal"] == "parent" and claims["parent_id"] is not None

    from apps.accounts.auth import SessionRefreshToken
    tok = SessionRefreshToken.for_user(User.objects.get(username="p_phone"))
    tok["session_exp"] = int(time.time()) - 10
    bad = APIClient().post("/api/auth/refresh/", {"refresh": str(tok)})
    assert bad.status_code == 401


@pytest.mark.django_db
def test_parent_logout_does_not_revoke_teacher_session():
    _setup()
    _, pdata = _parent_login()
    tdata = APIClient().post("/api/auth/trainer/login/",
                             {"username": "c_coach", "password": "pass12345"}).data

    # Parent logs out (blacklists only the parent refresh).
    pcl = _token_client(pdata["access"])
    assert pcl.post("/api/auth/logout/", {"refresh": pdata["refresh"]}).status_code == 200
    assert APIClient().post("/api/auth/refresh/",
                            {"refresh": pdata["refresh"]}).status_code == 401
    # Teacher's session is untouched.
    assert APIClient().post("/api/auth/refresh/",
                            {"refresh": tdata["refresh"]}).status_code == 200


# --- 8: existing shared login remains unchanged -----------------------------

@pytest.mark.django_db
def test_shared_login_still_issues_working_sessions():
    _setup()
    r = APIClient().post("/api/auth/login/", {"username": "c_coach", "password": "pass12345"})
    assert r.status_code == 200, r.content
    assert {"access", "refresh", "session_exp", "user"} <= set(r.data.keys())
    cl = _token_client(r.data["access"])
    assert cl.get("/api/auth/me/").status_code == 200
