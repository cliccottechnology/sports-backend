"""Parent mobile app reads: grant notifications + payment receipts.

The parent portal already exposes students, attendance, fees and sessions
(migration 0009). The mobile app also shows academy notifications and payment
receipts, and lets parents raise/track their own help-desk tickets.

Every one of these viewsets scopes parent-role accounts to their own data at
the queryset level (``scope_to_parent`` / raiser-only), so these grants cannot
leak other families' records. Non-destructive and idempotent.
"""
from django.db import migrations


PARENT_APP_VIEWS = [
    ("notifications.view", "View notifications", "notifications"),
    ("payments.view", "View payments", "payments"),
    ("support.view", "View support tickets", "support"),
    ("support.create", "Raise support tickets", "support"),
    ("support.reply", "Reply to support tickets", "support"),
]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name, module in PARENT_APP_VIEWS:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})
    role, _ = Role.objects.get_or_create(name="parent", defaults={"description": "Parent/Student"})
    ids = list(Permission.objects.filter(code__in=[c for c, _, _ in PARENT_APP_VIEWS]).values_list("id", flat=True))
    if ids:
        role.permissions.add(*ids)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0011_customization_permissions")]

    operations = [migrations.RunPython(grant, reverse_noop)]
