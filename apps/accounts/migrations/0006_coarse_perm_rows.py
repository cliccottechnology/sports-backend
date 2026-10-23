"""Ensure coarse permission rows referenced across the codebase exist.

`users.manage` and `sessions.manage` are used by viewsets, the seed's
academy_admin mapping, and rank logic — but no seed or migration ever created
their Permission rows. `filter(code__in=[...])` silently dropped them, so
academy admins were locked out of user and session management with no error.

Runtime expansion (`x.manage` -> all `<x>.*`) already handles these codes;
this migration only ensures the rows exist. Non-destructive: pure get_or_create.
"""
from django.db import migrations


def ensure_coarse_rows(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    for code, name, module in [
        ("users.manage", "Manage users (full access)", "users"),
        ("sessions.manage", "Manage sessions (full access)", "sessions"),
    ]:
        Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})


def reverse_noop(apps, schema_editor):
    # Keep the rows: deleting them would silently revoke grants again.
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_rbac_granular")]

    operations = [migrations.RunPython(ensure_coarse_rows, reverse_noop)]
