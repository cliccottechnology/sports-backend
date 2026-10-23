"""Full RBAC verification: Super Admin authority, view-only Admins, immediate
grant/revoke, self-protection, sensitive ops, audit, and data preservation."""
from datetime import date

import pytest
from rest_framework.test import APIClient


def mkuser(username, role=None, superuser=False, pwd="pass12345"):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, is_superuser=superuser)
    u.set_password(pwd)
    u.save()
    return u


def role_with(*codes, name="trole"):
    from apps.accounts.models import Permission, Role
    r, _ = Role.objects.get_or_create(name=name)
    r.permissions.add(*Permission.objects.filter(code__in=codes))
    return r


def auth_client(user, password="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": user.username, "password": password})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


@pytest.fixture
def hari():
    from apps.accounts.models import Role
    return mkuser("test_hari", role=Role.objects.get(name="super_admin"), superuser=True)


@pytest.mark.django_db
def test_1_super_admin_full_access(hari):
    cl = auth_client(hari)
    assert cl.get("/api/students/").status_code == 200
    assert cl.get("/api/users/").status_code == 200
    assert cl.get("/api/settings/").status_code == 200
    assert cl.get("/api/audit/logs/").status_code == 200
    r = cl.post("/api/students/", {"student_id": "RBAC-1", "first_name": "A",
                                   "enrollment_date": str(date.today())}, format="json")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_2_3_create_admin_view_only_default(hari):
    from apps.accounts.models import Role
    admin_cl = auth_client(hari)
    tpl = Role.objects.get(name="school_admin")
    assert tpl.permissions.count() == 14
    views = sorted(tpl.permissions.values_list("code", flat=True))
    assert all(c.endswith(".view") for c in views)
    clone = Role.objects.create(name="admin_newbie", description="t")
    clone.permissions.set(tpl.permissions.all())
    u = mkuser("newbie", role=clone)
    me = auth_client(u).get("/api/auth/me/").data
    assert "students.view" in me["permissions"]
    assert "students.create" not in me["permissions"]
    assert "users.view" not in me["permissions"]
    cl = auth_client(u)
    assert cl.get("/api/students/").status_code == 200
    assert cl.get("/api/users/").status_code == 403
    assert cl.post("/api/students/", {"student_id": "RBAC-X", "first_name": "A",
                                      "enrollment_date": str(date.today())}, format="json").status_code == 403


@pytest.mark.django_db
def test_4_module_gating_without_view():
    u = mkuser("noview", role=role_with("reports.view", name="r1"))
    cl = auth_client(u)
    assert cl.get("/api/students/").status_code == 403
    assert cl.get("/api/fees/").status_code == 403
    assert cl.get("/api/reports/dashboard/").status_code == 200


@pytest.mark.django_db
def test_5_6_no_create_edit_or_url_bypass():
    u = mkuser("vonly", role=role_with("students.view", "fees.view", name="r2"))
    cl = auth_client(u)
    assert cl.post("/api/students/", {"student_id": "RBAC-Y", "first_name": "A",
                                      "enrollment_date": str(date.today())}, format="json").status_code == 403
    s = cl.get("/api/students/").data["results"][0] if cl.get("/api/students/").data.get("results") else None
    if s:
        assert cl.patch(f"/api/students/{s['id']}/", {"first_name": "ZZ"}, format="json").status_code == 403
    assert cl.get("/api/fees/summary/").status_code == 200
    assert cl.post("/api/fees/mark_overdue/", {}, format="json").status_code == 403


@pytest.mark.django_db
def test_7_8_grant_then_revoke_immediate(hari):
    admin_cl = auth_client(hari)
    role = role_with("students.view", name="r3")
    u = mkuser("flex", role=role)
    cl = auth_client(u)
    payload = {"student_id": "RBAC-Z", "first_name": "A", "enrollment_date": str(date.today())}
    assert cl.post("/api/students/", payload, format="json").status_code == 403
    # HARI grants create on the same live token: next request passes.
    r = admin_cl.patch(f"/api/users/roles/{role.pk}/", {"permission_codes": ["students.view", "students.create"]}, format="json")
    assert r.status_code == 200, r.content
    assert cl.post("/api/students/", payload, format="json").status_code == 201
    # HARI revokes again: the very next request fails — no re-login needed.
    r = admin_cl.patch(f"/api/users/roles/{role.pk}/", {"permission_codes": ["students.view"]}, format="json")
    assert r.status_code == 200, r.content
    payload["student_id"] = "RBAC-Z2"
    assert cl.post("/api/students/", payload, format="json").status_code == 403


@pytest.mark.django_db
def test_9_deactivated_token_rejected(hari):
    admin_cl = auth_client(hari)
    u = mkuser("doomed", role=role_with("students.view", name="r4"))
    cl = auth_client(u)
    assert cl.get("/api/auth/me/").status_code == 200
    assert admin_cl.patch(f"/api/users/{u.pk}/", {"is_active": False}, format="json").status_code == 200
    assert cl.get("/api/auth/me/").status_code == 401
    assert cl.post("/api/auth/login/", {"username": "doomed", "password": "pass12345"}).status_code == 401


@pytest.mark.django_db
def test_10_11_self_protection(hari):
    from apps.accounts.models import Role
    cl = auth_client(hari)
    me = cl.get("/api/auth/me/").data
    other_role = Role.objects.get(name="academy_admin").pk
    assert cl.patch(f"/api/users/{me['id']}/", {"role": other_role}, format="json").status_code == 400
    assert cl.patch(f"/api/users/{me['id']}/", {"is_superuser": False}, format="json").status_code == 400
    assert cl.patch(f"/api/users/{me['id']}/", {"is_active": False}, format="json").status_code == 400
    assert cl.delete(f"/api/users/{me['id']}/").status_code in (400, 403)
    from apps.accounts.models import User
    assert User.objects.get(pk=me["id"]).is_active is True


@pytest.mark.django_db
def test_12_sensitive_ops_need_perms(hari):
    from apps.accounts.models import Role
    admin_cl = auth_client(hari)
    role = Role.objects.create(name="limited_admin")
    role.permissions.add(*[p for p in role.permissions.all()])  # none
    from apps.accounts.models import Permission
    role.permissions.add(*Permission.objects.filter(code__in=["students.view", "fees.view", "users.view"]))
    u = mkuser("limited", role=role)
    cl = auth_client(u)
    assert cl.post(f"/api/users/{u.pk}/reset_password/", {}, format="json").status_code == 403
    fee = cl.get("/api/fees/").data
    fee_id = (fee.get("results") or [{}])[0].get("id", 1)
    assert cl.post(f"/api/fees/{fee_id}/adjust/", {"amount": 1, "reason": "x"}, format="json").status_code == 403
    assert cl.get("/api/reports/students/?export=csv").status_code == 403
    assert cl.get("/api/reports/students/?format=json").status_code == 403


@pytest.mark.django_db
def test_13_audit_records_security_actions(hari):
    from apps.audit.models import AuditLog
    admin_cl = auth_client(hari)
    role = role_with("students.view", name="r5")
    u = mkuser("audited", role=role)
    admin_cl.patch(f"/api/users/roles/{role.pk}/", {"permission_codes": ["students.view", "reports.view"]}, format="json")
    assert AuditLog.objects.filter(action="role.update", record=f"role:{role.pk}").exists()
    r = admin_cl.post(f"/api/users/{u.pk}/reset_password/", {}, format="json")
    assert r.status_code == 200, r.content
    assert "temporary_password" in r.data
    log = AuditLog.objects.filter(action="user.password_reset", record=f"user:{u.pk}").first()
    assert log is not None
    assert "temporary_password" not in str(log.new_value)
    assert admin_cl.patch(f"/api/users/{u.pk}/", {"is_active": False}, format="json").status_code == 200
    assert AuditLog.objects.filter(action="user.update", record=f"user:{u.pk}").exists()


@pytest.mark.django_db
def test_14_password_first_login_flow(hari):
    admin_cl = auth_client(hari)
    role = role_with("students.view", name="r6")
    u = mkuser("tempuser", role=role)
    r = admin_cl.post(f"/api/users/{u.pk}/reset_password/", {}, format="json")
    temp = r.data["temporary_password"]
    assert len(temp) >= 8
    cl = APIClient()
    lr = cl.post("/api/auth/login/", {"username": "tempuser", "password": temp})
    assert lr.status_code == 200
    assert lr.data["user"]["must_change_password"] is True
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {lr.data['access']}")
    assert cl.post("/api/auth/change-password/", {"old_password": "wrong", "new_password": "newpass123"}).status_code == 400
    assert cl.post("/api/auth/change-password/", {"old_password": temp, "new_password": "newpass123"}).status_code == 200
    assert cl.get("/api/auth/me/").data["must_change_password"] is False
    anon = APIClient()
    assert anon.post("/api/auth/login/", {"username": "tempuser", "password": "newpass123"}).status_code == 200
    assert anon.post("/api/auth/login/", {"username": "tempuser", "password": temp}).status_code == 401


@pytest.mark.django_db
def test_15_existing_modules_still_work(hari):
    cl = auth_client(hari)
    assert cl.get("/api/sports/sports/").status_code == 200
    assert cl.get("/api/sessions/my_sessions/").status_code == 200
    assert cl.get("/api/attendance/").status_code == 200
    assert cl.get("/api/trainer-fees/payables/summary/").status_code == 200
    assert cl.get("/api/whatsapp/status/").status_code == 200
    assert cl.get("/api/notifications/").status_code == 200
    assert cl.get("/api/documents/").status_code == 200


@pytest.mark.django_db
def test_export_works_with_perm_and_fails_without(hari):
    admin_cl = auth_client(hari)
    role = role_with("reports.view", name="r7")
    u = mkuser("exporter", role=role)
    cl = auth_client(u)
    assert cl.get("/api/reports/students/?export=csv").status_code == 403
    admin_cl.patch(f"/api/users/roles/{role.pk}/",
                   {"permission_codes": ["reports.view", "reports.export"]}, format="json")
    r = cl.get("/api/reports/students/?export=csv")
    assert r.status_code == 200, r.content
    assert "text/csv" in r["Content-Type"]


@pytest.mark.django_db
def test_16_data_preserved_and_hari_intact():
    from apps.accounts.models import Permission, Role, User
    from apps.accounts.rbac_catalog import ALL_CODES
    assert Permission.objects.count() >= len(ALL_CODES)
    assert Role.objects.filter(name="school_admin").exists()
    assert Role.objects.filter(name__in=["super_admin", "academy_admin", "coach", "parent"]).count() == 4
    sa = Role.objects.get(name="super_admin")
    assert set(sa.permissions.values_list("code", flat=True)) >= {c for c, _, _ in ALL_CODES}
    # A super-admin login keeps working with full power after the rollout.
    u = mkuser("sa_check", role=sa)
    assert set(u.perm_codes()) >= {c for c, _, _ in ALL_CODES}
    assert User.objects.filter(username="sa_check").exists()


@pytest.mark.django_db
def test_users_endpoint_honours_granular_codes(hari):
    """Roles store granular `users.*` codes (the coarse `users.manage` row is
    optional), so list/create/edit must key off their own action codes."""
    from apps.accounts.models import Role
    role = role_with("users.view", name="uv")
    cl = auth_client(mkuser("uv_reader", role=role))
    assert cl.get("/api/users/").status_code == 200
    assert cl.post("/api/users/", {"username": "uv_denied", "password": "pass12345"},
                   format="json").status_code == 403

    editor_role = role_with("users.view", "users.create", "users.edit", name="uw")
    ecl = auth_client(mkuser("uw_editor", role=editor_role))
    assert ecl.get("/api/users/").status_code == 200
    coach = Role.objects.get(name="coach")
    r = ecl.post("/api/users/", {"username": "uw_created", "password": "pass12345", "role": coach.pk},
                 format="json")
    assert r.status_code == 201, r.content
    from apps.accounts.models import User
    created = User.objects.get(username="uw_created")
    assert ecl.patch(f"/api/users/{created.pk}/", {"first_name": "Granular"},
                     format="json").status_code == 200
    assert ecl.post(f"/api/users/{created.pk}/reset_password/", {}, format="json").status_code == 403


@pytest.mark.django_db
def test_settings_endpoint_honours_granular_codes(hari):
    viewer_role = role_with("settings.view", name="sv")
    vcl = auth_client(mkuser("sv_reader", role=viewer_role))
    assert vcl.get("/api/settings/").status_code == 200
    assert vcl.post("/api/settings/", {"key": "sv.denied", "value": "1"},
                    format="json").status_code == 403

    editor_role = role_with("settings.view", "settings.edit", name="se")
    ecl = auth_client(mkuser("se_editor", role=editor_role))
    assert ecl.get("/api/settings/").status_code == 200
    r = ecl.post("/api/settings/", {"key": "se.created", "value": "1"}, format="json")
    assert r.status_code == 201, r.content
    assert ecl.patch(f"/api/settings/{r.data['id']}/", {"value": "2"}, format="json").status_code == 200


@pytest.mark.django_db
def test_role_manager_cannot_escalate_privileges():
    """A non-super-admin holding roles.manage may shape custom roles only within
    its own power — it can never mint a role granting codes it lacks, nor grant
    platform-only codes (schools.manage / roles.manage)."""
    from apps.accounts.models import Role
    mgr_role = role_with("roles.view", "roles.manage", "reports.view", name="r8")
    mgr = mkuser("role_mgr", role=mgr_role)
    cl = auth_client(mgr)

    # Within own power: allowed.
    ok = cl.post("/api/users/roles/", {"name": "clone_ok", "permission_codes": ["reports.view"]},
                 format="json")
    assert ok.status_code == 201, ok.content

    # A code the requester does not hold: refused.
    assert cl.post("/api/users/roles/", {"name": "escalate", "permission_codes": ["payments.refund"]},
                   format="json").status_code == 403
    # Platform-only codes: refused even though roles.manage is held.
    assert cl.post("/api/users/roles/", {"name": "platform", "permission_codes": ["schools.manage"]},
                   format="json").status_code == 403
    assert cl.post("/api/users/roles/", {"name": "propagate", "permission_codes": ["roles.manage"]},
                   format="json").status_code == 403

    # Widening an existing custom role with an ungrantable code is refused too.
    target = Role.objects.create(name="widen_me")
    assert cl.patch(f"/api/users/roles/{target.pk}/", {"permission_codes": ["students.create"]},
                    format="json").status_code == 403
