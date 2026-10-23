"""Customization request workflow permissions.

Creates the customization.* permission rows, grants the full set to the
super_admin role, and lets academy admins submit requests, comment, and track
their own requests. Review/approve/manage stay with the Super Admin (or
whoever is explicitly granted them). Non-destructive and idempotent.
"""
from django.db import migrations


CUSTOMIZATION_CODES = [
    ("customization.view", "View customization requests"),
    ("customization.create", "Submit customization requests"),
    ("customization.reply", "Comment / reply on requests"),
    ("customization.review", "Review, approve, reject, clarify, prioritize"),
    ("customization.manage", "Assign work, track progress, reopen requests"),
]

ACADEMY_CODES = ["customization.view", "customization.create", "customization.reply"]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name in CUSTOMIZATION_CODES:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": "customization"})

    super_admin, _ = Role.objects.get_or_create(name="super_admin", defaults={"description": "Super Admin"})
    super_admin.permissions.add(*Permission.objects.filter(code__in=[c for c, _ in CUSTOMIZATION_CODES]))

    academy, _ = Role.objects.get_or_create(name="academy_admin", defaults={"description": "Academy Management/Admin"})
    academy.permissions.add(*Permission.objects.filter(code__in=ACADEMY_CODES))


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0010_support_permissions")]

    operations = [migrations.RunPython(grant, reverse_noop)]
