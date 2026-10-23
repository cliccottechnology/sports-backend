"""Parent portal reads: grant view rights; row isolation stays queryset-enforced.

Parents may list their OWN students, attendance, fees and sessions. Every one
of those viewsets filters parent-role accounts to linked students, so these
grants cannot leak other families' data. Non-destructive and idempotent.
"""
from django.db import migrations


PARENT_VIEWS = ["students.view", "attendance.view", "fees.view", "sessions.view"]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    role, _ = Role.objects.get_or_create(name="parent", defaults={"description": "Parent/Student"})
    ids = list(Permission.objects.filter(code__in=PARENT_VIEWS).values_list("id", flat=True))
    if ids:
        role.permissions.add(*ids)


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0008_school_permissions")]

    operations = [migrations.RunPython(grant, reverse_noop)]
