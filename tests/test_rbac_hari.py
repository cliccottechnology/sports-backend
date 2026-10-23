"""HARI Super Admin hierarchy: omnipotence, view-only defaults, immediate
grant/revoke, self-modify blocks, sensitive gates, audit trail, bootstrap."""
import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Permission, Role, User


def _mkuser(username, role=None, pwd="pass12345", **kw):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, **kw)
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
    from apps.accounts.models import Permission, Role
    role, _ = Role.objects.get_or_create(name=name)
    role.permissions.set(Permission.objects.filter(code__in=codes))
    return role


@pytest.mark.django_db
def test_hari_bootstrap_and_omnipotence():
    from django.core.management import call_command
    from apps.accounts.models import Role, User
    other = _mkuser("bystander")
    call_command("ensure_hari", password="Hari@12345")
    hari = User.objects.get(username="hari")
    assert hari.is_superuser and hari.is_active and hari.role.name == "super_admin"
    assert User.objects.get(username="bystander").role is None  # untouched
    call_command("ensure_hari")  # idempotent, no leak/reset
    hari.refresh_from_db()
    assert hari.check_password("Hari@12345")

    cl = _login("hari", "Hari@12345")
    for url in ("/api/students/", "/api/fees/", "/api/payments/",
                "/api/trainer-fees/payables/", "/api/whatsapp/messages/",
                "/api/notifications/", "/api/audit/logs/", "/api/users/",
                "/api/settings/", "/api/reports/dashboard/", "/api/sessions/",
                "/api/batches/", "/api/coaches/", "/api/parents/",
                "/api/documents/", "/api/attendance/"):
        assert cl.get(url).status_code == 200, url


@pytest.mark.django_db
def test_new_admin_view_only_by_default():
    from apps.accounts.rbac_catalog import SCHOOL_ADMIN_VIEWS
    from apps.accounts.models import Role
    school, _ = Role.objects.get_or_create(name="school_admin")
    held = set(school.permissions.values_list("code", flat=True))
    assert held == set(SCHOOL_ADMIN_VIEWS), held ^ set(SCHOOL_ADMIN_VIEWS)
    assert all(c.endswith(".view") for c in held)


@pytest.mark.django_db
def test_view_only_blocked_from_writes_and_grant_revoke_immediate():
    from datetime import date
    from apps.students.models import Student
    _role("viewer", ["students.view"])
    _mkuser("viewer1", Role.objects.get(name="viewer"))
    cl = _login("viewer1")
    assert cl.get("/api/students/").status_code == 200
    payload = {"student_id": "V-1", "first_name": "V", "enrollment_date": str(date.today())}
    assert cl.post("/api/students/", payload, format="json").status_code == 403
    # HARI grants create -> same token works at once
    Role.objects.get(name="viewer").permissions.add(*Permission.objects.filter(code__in=["students.create"]))
    r = cl.post("/api/students/", payload, format="json")
    assert r.status_code == 201, r.content
    assert Student.objects.filter(student_id="V-1").exists()
    # HARI revokes again -> same token blocked on the next request
    Role.objects.get(name="viewer").permissions.remove(*Permission.objects.filter(code__in=["students.create"]))
    payload["student_id"] = "V-2"
    assert cl.post("/api/students/", payload, format="json").status_code == 403
    assert not Student.objects.filter(student_id="V-2").exists()


@pytest.mark.django_db
def test_non_super_cannot_touch_super_admin_or_self_role():
    admin_role, _ = Role.objects.get_or_create(name="aa_tmp")
    admin_role.permissions.set(Permission.objects.filter(code__in=["users.manage"]))
    boss = _mkuser("boss2", admin_role)
    sa_role, _ = Role.objects.get_or_create(name="super_admin")
    sa = _mkuser("sa2", sa_role)
    sa.is_superuser = True
    sa.save()
    cl = _login("boss2")
    # touch a super admin -> blocked
    assert cl.patch(f"/api/users/{sa.id}/", {"first_name": "X"}, format="json").status_code == 403
    # own role change -> blocked
    assert cl.patch(f"/api/users/{boss.id}/", {"role": sa_role.id}, format="json").status_code == 400
    boss.refresh_from_db()
    assert boss.role_id == admin_role.id


@pytest.mark.django_db
def test_sensitive_fee_adjust_needs_grant_and_audits():
    from datetime import date
    from apps.audit.models import AuditLog
    from apps.fees.models import FeePlan, StudentFee
    from apps.students.models import Student
    _role("feeviewer", ["fees.view"])
    _mkuser("feeviewer1", Role.objects.get(name="feeviewer"))
    cl = _login("feeviewer1")
    plan = FeePlan.objects.create(name="AdjPlan", cycle="monthly", amount=2000)
    st = Student.objects.create(student_id="ADJ-1", first_name="A", enrollment_date=date.today())
    fee = StudentFee.objects.create(student=st, fee_plan=plan, period_label="2026-10", amount=2000, due_date=date.today())
    url = f"/api/fees/{fee.id}/adjust/"
    assert cl.post(url, {"amount": "500", "reason": "mid-month"}, format="json").status_code == 403
    Role.objects.get(name="feeviewer").permissions.add(*Permission.objects.filter(code__in=["fees.adjust"]))
    r = cl.post(url, {"amount": "500", "reason": "mid-month"}, format="json")
    assert r.status_code == 201, r.content
    fee.refresh_from_db()
    assert float(fee.discount) == 500.0
    assert AuditLog.objects.filter(action="fee.adjust", record=f"studentfee:{fee.pk}").exists()


@pytest.mark.django_db
def test_password_reset_forces_change():
    mgr_role, _ = Role.objects.get_or_create(name="pw_mgr")
    mgr_role.permissions.set(Permission.objects.filter(code__in=["users.manage", "users.reset_password"]))
    assert set(mgr_role.permissions.values_list("code", flat=True)) == {"users.manage", "users.reset_password"}
    mgr = _mkuser("pwmgr", mgr_role)
    coach_role, _ = Role.objects.get_or_create(name="coach")
    target = _mkuser("pwtarget", coach_role, pwd="oldpass123")
    cl = _login("pwmgr")
    r = cl.post(f"/api/users/{target.id}/reset_password/", {}, format="json")
    assert r.status_code == 200, r.content
    temp = r.data["temporary_password"]
    target.refresh_from_db()
    assert target.must_change_password is True
    tcl = _login("pwtarget", temp)
    assert tcl.get("/api/auth/me/").data["must_change_password"] is True
    r = tcl.post("/api/auth/change-password/", {"old_password": temp, "new_password": "brandnew123"}, format="json")
    assert r.status_code == 200, r.content
    assert tcl.get("/api/auth/me/").data["must_change_password"] is False
