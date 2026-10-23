"""Give the coach role the attendance-marking permission.

A trainer/coach must be able to open each of their assigned sessions and
manually mark every student present / absent / late / leave. That action is
`POST /api/attendance/bulk_mark/`, which requires `attendance.mark`.

Previously `attendance.mark` was only ever granted by the `seed_demo`
management command, so a real (migrated) installation left the coach role
without it and every marking attempt returned 403. This makes it part of the
role itself, and grants `attendance.view` alongside it so the coach can read
back the roster they are marking.

Non-destructive and idempotent.
"""
from django.db import migrations

COACH_ATTENDANCE = ["attendance.view", "attendance.mark"]

DESCRIPTIONS = {
    "attendance.view": "View attendance",
    "attendance.mark": "Mark attendance",
}


def grant(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code in COACH_ATTENDANCE:
        Permission.objects.get_or_create(
            code=code, defaults={"name": DESCRIPTIONS[code], "module": "attendance"})

    coach, _ = Role.objects.get_or_create(name="coach", defaults={"description": "Coach / Trainer"})
    coach.permissions.add(*Permission.objects.filter(code__in=COACH_ATTENDANCE))


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0013_branch_permissions")]

    operations = [migrations.RunPython(grant, reverse_noop)]
