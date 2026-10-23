"""Give the coach role the sports-view permission.

The trainer app's Sessions screen loads the sport filter from
`GET /api/sports/sports/`, which requires `sports.view`. The coach role was
only ever granted that code by the `seed_demo` management command (via the
legacy `masters.view` mapping), so a real (migrated) installation left the
coach role without it and the Sessions screen silently failed to load.

This makes it part of the role itself. Non-destructive and idempotent.
"""
from django.db import migrations

COACH_SPORTS = ["sports.view"]


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code in COACH_SPORTS:
        Permission.objects.get_or_create(
            code=code, defaults={"name": "View sports", "module": "sports"})

    coach, _ = Role.objects.get_or_create(name="coach", defaults={"description": "Coach / Trainer"})
    coach.permissions.add(*Permission.objects.filter(code__in=COACH_SPORTS))


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0014_coach_attendance_mark")]

    operations = [migrations.RunPython(grant, reverse_noop)]
