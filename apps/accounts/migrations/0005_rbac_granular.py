"""RBAC granular rollout (data-preserving).

- Creates every canonical <module>.<action> permission row.
- Grants the full set to the super_admin role.
- Maps existing legacy grants (students.manage, masters.view, …) onto their
  granular equivalents for academy_admin / coach / parent roles.
  Legacy rows are kept, so nothing previously granted is lost.
- Creates the view-only `school_admin` template role for new Admin accounts.
- Marks holders of the super_admin role as Django superusers (belt and braces;
  only accounts already in that role — nobody else is promoted).
"""
from django.db import migrations


def rollout(apps, schema_editor):
    from apps.accounts.rbac_catalog import ALL_CODES, LEGACY_MAP, SCHOOL_ADMIN_VIEWS
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    User = apps.get_model("accounts", "User")

    for code, name, module in ALL_CODES:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})

    def grant(role, codes):
        ids = list(Permission.objects.filter(code__in=codes).values_list("id", flat=True))
        role.permissions.add(*ids)

    super_admin, _ = Role.objects.get_or_create(name="super_admin", defaults={"description": "Super Admin"})
    grant(super_admin, [c for c, _, _ in ALL_CODES])

    academy_admin, _ = Role.objects.get_or_create(name="academy_admin", defaults={"description": "Academy Management/Admin"})
    mapped = set()
    for rp in academy_admin.permissions.all().values_list("code", flat=True):
        mapped.update(LEGACY_MAP.get(rp, [rp]))
    grant(academy_admin, mapped)

    for rname in ("coach", "parent"):
        role, _ = Role.objects.get_or_create(name=rname, defaults={"description": rname})
        extra = {"coach": ["sessions.view", "students.view", "trainer_fees.view", "notifications.view"],
                 "parent": []}[rname]
        mapped = set(extra)
        for rp in role.permissions.all().values_list("code", flat=True):
            mapped.update(LEGACY_MAP.get(rp, [rp]))
        grant(role, mapped)

    Role.objects.get_or_create(name="school_admin", defaults={"description": "School Admin (view-only by default)"})
    school = Role.objects.get(name="school_admin")
    if school.permissions.count() == 0:
        grant(school, SCHOOL_ADMIN_VIEWS)

    User.objects.filter(role=super_admin).update(is_superuser=True)


def reverse_rollout(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(name="school_admin").delete()


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_user_must_change_password_alter_role_name")]

    operations = [migrations.RunPython(rollout, reverse_rollout)]
