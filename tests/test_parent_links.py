"""Parent <-> student links: create, duplicate-block, unlink. Records always survive."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def admin_client():
    from apps.accounts.models import Permission, Role, User
    cl = APIClient()
    role, _ = Role.objects.get_or_create(name="link_admin")
    for code in ("students.manage",):
        perm, _ = Permission.objects.get_or_create(code=code, defaults={"name": code, "module": "students"})
        role.permissions.add(perm)
    u = User(username="linker", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    r = cl.post("/api/auth/login/", {"username": "linker", "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


@pytest.mark.django_db
def test_parent_student_link_roundtrip(admin_client):
    from datetime import date
    from apps.parents.models import Parent, StudentParent
    from apps.students.models import Student
    st = Student.objects.create(student_id="L-S1", first_name="Link", enrollment_date=date.today())
    p = Parent.objects.create(first_name="Mom", relationship="mother", mobile="9000000001")

    r = admin_client.post("/api/parents/links/list/", {"student": st.id, "parent": p.id, "is_primary": True}, format="json")
    assert r.status_code == 201, r.content
    link_id = r.data["id"]

    r = admin_client.get("/api/parents/links/list/", {"parent": p.id})
    assert r.status_code == 200, r.content
    rows = r.data["results"] if isinstance(r.data, dict) else r.data
    assert any(x["student"] == st.id for x in rows)

    # duplicate pair is rejected, not duplicated
    r = admin_client.post("/api/parents/links/list/", {"student": st.id, "parent": p.id}, format="json")
    assert r.status_code == 400, r.content
    assert StudentParent.objects.filter(student=st, parent=p).count() == 1

    # unlink removes only the link
    r = admin_client.delete(f"/api/parents/links/list/{link_id}/")
    assert r.status_code in (200, 204), r.content
    assert StudentParent.objects.filter(pk=link_id).count() == 0
    assert Parent.objects.filter(pk=p.id).count() == 1
    assert Student.objects.filter(pk=st.id).count() == 1
