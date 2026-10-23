"""Run the trainer attendance automation sweep.

Intended to be scheduled externally (cron / Windows Task Scheduler / a Docker
sidecar loop). Idempotent — safe to run as often as every few minutes.

Examples:
    python manage.py process_trainer_attendance
    python manage.py process_trainer_attendance --school 3
    python manage.py process_trainer_attendance --dry-run
"""
from django.core.management.base import BaseCommand

from apps.coaches.attendance_automation import process_trainer_attendance


class Command(BaseCommand):
    help = "Auto-mark absent trainers who missed the attendance deadline and send missed-attendance notifications."

    def add_arguments(self, parser):
        parser.add_argument("--school", type=int, default=None,
                            help="Only process one academy (school id).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would happen without writing anything.")

    def handle(self, *args, **options):
        result = process_trainer_attendance(school_id=options["school"],
                                            dry_run=options["dry_run"])
        prefix = "[dry-run] " if result.get("dry_run") else ""
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}considered={result['considered']} auto_absent={result['auto_absent']} "
            f"reminders={result['reminders']} notifications={result['notifications']} "
            f"skipped(existing={result['skipped_existing']} leave={result['skipped_leave']} "
            f"not_due={result['skipped_not_due']})"))
