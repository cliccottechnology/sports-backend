"""User edits apply everywhere, immediately: deactivation kills live tokens,
password resets re-hash, self-deactivation is blocked, role swaps re-scope."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def managing_client():
    from apps.accounts.models import Permission, Role, User
    cl = APIClient()
    role, _ = Role.objects.get_or_create(name="user_admin")
    perm, _ = Permission.objects.get_or_create(code="users.manage", defaults={"name": "x", "module": "users"})
    role.permissions.add(perm)
    admin = User(username="userboss", role=role, is_active=True)
    admin.set_password("pass12345")
    admin.save()
    r = cl.post("/api/auth/login/", {"username": "userboss", "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl, admin


@pytest.mark.django_db
def test_deactivation_kills_live_token(managing_client):
    cl, _ = managing_client
    from apps.accounts.models import Role, User
    role, _ = Role.objects.get_or_create(name="victim_role")
    u = User(username="victim", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    victim = APIClient()
    r = victim.post("/api/auth/login/", {"username": "victim", "password": "pass12345"})
    assert r.status_code == 200, r.content
    victim.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    assert victim.get("/api/auth/me/").status_code == 200

    r = cl.patch(f"/api/users/{u.id}/", {"is_active": False}, format="json")
    assert r.status_code == 200, r.content
    # Same token, next request: rejected immediately (admin + trainer app alike).
    assert victim.get("/api/auth/me/").status_code == 401


@pytest.mark.django_db
def test_password_reset_rehashes(managing_client):
    cl, _ = managing_client
    from apps.accounts.models import Role, User
    role, _ = Role.objects.get_or_create(name="pw_role")
    u = User(username="pwuser", role=role, is_active=True)
    u.set_password("oldpass123")
    u.save()
    r = cl.patch(f"/api/users/{u.id}/", {"password": "newpass123"}, format="json")
    assert r.status_code == 200, r.content
    assert "password" not in r.data  # write-only: never leaked
    anon = APIClient()
    assert anon.post("/api/auth/login/", {"username": "pwuser", "password": "newpass123"}).status_code == 200
    assert anon.post("/api/auth/login/", {"username": "pwuser", "password": "oldpass123"}).status_code == 401


@pytest.mark.django_db
def test_cannot_deactivate_self(managing_client):
    cl, admin = managing_client
    r = cl.patch(f"/api/users/{admin.id}/", {"is_active": False}, format="json")
    assert r.status_code == 400, r.content
    admin.refresh_from_db()
    assert admin.is_active is True


@pytest.mark.django_db
def test_role_change_rescopes_immediately(managing_client):
    cl, _ = managing_client
    from apps.accounts.models import Permission, Role, User
    full, _ = Role.objects.get_or_create(name="full_role")
    perm, _ = Permission.objects.get_or_create(code="students.manage", defaults={"name": "x", "module": "students"})
    pcreate, _ = Permission.objects.get_or_create(code="parents.create", defaults={"name": "y", "module": "parents"})
    full.permissions.add(perm, pcreate)
    u = User(username="scoped", role=full, is_active=True)
    u.set_password("pass12345")
    u.save()
    scoped = APIClient()
    r = scoped.post("/api/auth/login/", {"username": "scoped", "password": "pass12345"})
    scoped.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    assert "students.manage" in scoped.get("/api/auth/me/").data["permissions"]
    assert scoped.post("/api/parents/", {"first_name": "T", "relationship": "father", "mobile": "1"}).status_code == 201

    empty, _ = Role.objects.get_or_create(name="empty_role")
    r = cl.patch(f"/api/users/{u.id}/", {"role": empty.id}, format="json")
    assert r.status_code == 200, r.content
    # Same token, next request: new (empty) permissions apply at once.
    assert "students.manage" not in scoped.get("/api/auth/me/").data["permissions"]
    assert scoped.post("/api/parents/", {"first_name": "T", "relationship": "father", "mobile": "1"}).status_code == 403
