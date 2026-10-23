"""Role hierarchy: super admin delegates; academy admins can't escalate."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def hierarchy():
    from apps.accounts.models import Permission, Role, User
    perm, _ = Permission.objects.get_or_create(code="users.manage",
                                               defaults={"name": "users", "module": "users"})
    roles = {}
    for name in ("super_admin", "academy_admin", "coach"):
        r, _ = Role.objects.get_or_create(name=name)
        r.permissions.add(perm)
        roles[name] = r

    def mk(username, role):
        u = User(username=username, role=roles[role], is_active=True)
        u.set_password("pass12345")
        u.save()
        return u

    return {"roles": roles, "super": mk("h_super", "super_admin"),
            "admin": mk("h_admin", "academy_admin"), "coach": mk("h_coach", "coach")}


def _post(client, user, body):
    client.force_authenticate(user)
    return client.post("/api/users/", body, format="json")


@pytest.mark.django_db
def test_academy_admin_can_create_coach(hierarchy):
    client = APIClient()
    r = _post(client, hierarchy["admin"], {"username": "new_coach", "password": "pass12345",
                                           "role": hierarchy["roles"]["coach"].pk})
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_academy_admin_cannot_create_admin_or_super(hierarchy):
    client = APIClient()
    for role in ("academy_admin", "super_admin"):
        r = _post(client, hierarchy["admin"], {"username": f"no_{role}", "password": "pass12345",
                                               "role": hierarchy["roles"][role].pk})
        assert r.status_code == 403, (role, r.content)


@pytest.mark.django_db
def test_super_admin_can_create_academy_admin(hierarchy):
    client = APIClient()
    r = _post(client, hierarchy["super"], {"username": "school_admin", "password": "pass12345",
                                           "role": hierarchy["roles"]["academy_admin"].pk})
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_academy_admin_cannot_promote_to_super(hierarchy):
    client = APIClient()
    client.force_authenticate(hierarchy["admin"])
    r = client.patch(f"/api/users/{hierarchy['coach'].pk}/",
                     {"role": hierarchy["roles"]["super_admin"].pk}, format="json")
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_academy_admin_cannot_touch_super_account(hierarchy):
    client = APIClient()
    client.force_authenticate(hierarchy["admin"])
    r = client.patch(f"/api/users/{hierarchy['super'].pk}/", {"first_name": "X"}, format="json")
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_academy_admin_cannot_redefine_roles(hierarchy):
    client = APIClient()
    client.force_authenticate(hierarchy["admin"])
    r = client.post("/api/users/roles/", {"name": "god", "description": "x"}, format="json")
    assert r.status_code == 403, r.content
