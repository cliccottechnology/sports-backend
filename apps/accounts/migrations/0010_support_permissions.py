"""Support tickets module permissions.

Creates the support.* permission rows, grants the full set to the super_admin
role, and lets academy admins raise/reply to tickets. Row visibility is
queryset-enforced (raisers see only their own; support.manage sees all).
Non-destructive and idempotent.
"""
from django.db import migrations


SUPPORT_CODES = [
    ("support.view", "View support tickets"),
    ("support.create", "Raise support tickets"),
    ("support.reply", "Reply to tickets"),
    ("support.manage", "Manage all tickets (assign, status)"),
]

ACADEMY_CODES = ["support.view", "support.create", "support.reply"]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name in SUPPORT_CODES:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": "support"})

    super_admin, _ = Role.objects.get_or_create(name="super_admin", defaults={"description": "Super Admin"})
    super_admin.permissions.add(*Permission.objects.filter(code__in=[c for c, _ in SUPPORT_CODES]))

    academy, _ = Role.objects.get_or_create(name="academy_admin", defaults={"description": "Academy Management/Admin"})
    academy.permissions.add(*Permission.objects.filter(code__in=ACADEMY_CODES))


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_parent_portal_views")]

    operations = [migrations.RunPython(grant, reverse_noop)]
