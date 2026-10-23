"""Granular branch permissions.

The `schools` module previously exposed only `view` and `manage`, which made
branch CRUD unreachable for anyone who is not a Super Admin (BranchViewSet
wants `schools.create`). Splitting the platform act (`schools.manage` =
onboard/alter an academy) from the per-academy act (managing that academy's
own branches and locations) so a tenant administrator can run a multi-branch
academy without being able to touch another one.

Non-destructive and idempotent.
"""
from django.db import migrations

BRANCH_CODES = [
    ("schools.view", "View schools and branches"),
    ("schools.create", "Create branches"),
    ("schools.edit", "Edit branches"),
    ("schools.archive", "Archive branches"),
]

ACADEMY_CODES = ["schools.view", "schools.create", "schools.edit", "schools.archive"]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name in BRANCH_CODES:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": "schools"})

    super_admin, _ = Role.objects.get_or_create(name="super_admin", defaults={"description": "Super Admin"})
    super_admin.permissions.add(*Permission.objects.filter(code__in=[c for c, _ in BRANCH_CODES]))

    # A tenant academy admin must be able to maintain their own branches.
    academy, _ = Role.objects.get_or_create(name="academy_admin",
                                             defaults={"description": "Academy Management/Admin"})
    academy.permissions.add(*Permission.objects.filter(code__in=ACADEMY_CODES))


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0012_parent_app_views")]

    operations = [migrations.RunPython(grant, reverse_noop)]
