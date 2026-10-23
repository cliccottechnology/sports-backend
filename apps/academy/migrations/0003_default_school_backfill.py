"""Backfill: every pre-existing student/batch/coach belongs to the default school.

Users keep school=NULL (legacy all-access) — only newly onboarded school
admins get a school assigned.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    School = apps.get_model("academy", "School")
    school, _ = School.objects.get_or_create(
        name="HARI Sports Academy",
        defaults={"code": "HARI-MAIN", "is_active": True})
    for model, field in (("students", "Student"), ("batches", "Batch"), ("coaches", "Coach")):
        M = apps.get_model(model, field)
        M.objects.filter(school__isnull=True).update(school=school)


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("academy", "0002_school"),
        ("students", "0005_student_school"),
        ("batches", "0003_alter_batch_unique_together_batch_school_and_more"),
        ("coaches", "0005_coach_school"),
    ]

    operations = [migrations.RunPython(backfill, reverse_backfill)]
