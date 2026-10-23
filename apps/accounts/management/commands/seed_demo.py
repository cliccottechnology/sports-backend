"""Seed demo data (no real client data). Run: python manage.py seed_demo"""
from django.core.management.base import BaseCommand


PERMISSIONS = [
    ("users.manage", "Manage users (full access)", "users"),
    ("sessions.manage", "Manage sessions (full access)", "sessions"),
    ("masters.view", "View masters", "masters"),
    ("masters.manage", "Manage masters", "masters"),
    ("students.manage", "Manage students", "students"),
    ("coaches.manage", "Manage coaches", "coaches"),
    ("batches.manage", "Manage batches", "batches"),
    ("sessions.manage", "Manage sessions", "sessions"),
    ("attendance.mark", "Mark attendance", "attendance"),
    ("attendance.correct", "Correct attendance", "attendance"),
    ("fees.manage", "Manage fees", "fees"),
    ("payments.verify", "Verify payments", "payments"),
    ("trainer_fees.manage", "Manage trainer fees", "trainer_fees"),
    ("reports.view", "View reports", "reports"),
    ("reports.export", "Export reports", "reports"),
    ("notifications.send", "Send notifications", "notifications"),
    ("settings.manage", "Manage settings", "settings"),
    ("audit.view", "View audit logs", "audit"),
]

# Granular codes are the source of truth (apps/accounts/rbac_catalog.py);
# the legacy rows above stay for backward compatibility.
SCHOOL_ADMIN_VIEWS = [
    "sports.view", "students.view", "parents.view", "coaches.view", "batches.view",
    "sessions.view", "attendance.view", "fees.view", "payments.view", "trainer_fees.view",
    "whatsapp.view", "reports.view", "notifications.view", "documents.view",
]

ROLE_PERMS = {
    "super_admin": "*",
    "academy_admin": ["masters.view", "masters.manage", "students.manage", "coaches.manage", "batches.manage",
                      "sessions.manage", "attendance.mark", "attendance.correct", "fees.manage", "payments.verify",
                      "trainer_fees.manage", "reports.view", "reports.export", "notifications.send",
                      "settings.manage", "audit.view", "users.manage"],
    "coach": ["masters.view", "attendance.mark", "reports.view"],
    "parent": ["reports.view", "students.view", "attendance.view", "fees.view", "sessions.view"],
}


class Command(BaseCommand):
    help = "Seed roles, permissions, sports and demo records."

    def handle(self, *args, **options):
        from datetime import date, time, timedelta
        from apps.academy.models import SessionType, Sport, TrainingLocation
        from apps.accounts.models import Permission, Role, User
        from apps.batches.models import Batch, BatchStudent
        from apps.coaches.models import Coach, CoachSport
        from apps.fees.models import FeePlan, StudentFee
        from apps.sessions.models import Session
        from apps.parents.models import Parent, StudentParent
        from apps.students.models import Student
        from apps.whatsapp.models import WhatsAppTemplate

        for code, name, module in PERMISSIONS:
            Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})
        # Granular catalog rows (idempotent; see migration 0005 on existing DBs).
        from apps.accounts.rbac_catalog import ALL_CODES
        for code, name, module in ALL_CODES:
            Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})
        roles = {}
        for rname in ("super_admin", "academy_admin", "coach", "parent"):
            role, _ = Role.objects.get_or_create(name=rname, defaults={"description": rname})
            roles[rname] = role
            perms = ROLE_PERMS[rname]
            if perms == "*":
                role.permissions.set(Permission.objects.all())
            else:
                # Additive on purpose: re-running the seed must never strip
                # permissions a Super Admin granted later.
                role.permissions.add(*Permission.objects.filter(code__in=perms))
        school, _ = Role.objects.get_or_create(name="school_admin", defaults={"description": "School Admin (view-only by default)"})
        if school.permissions.count() == 0:
            school.permissions.set(Permission.objects.filter(code__in=SCHOOL_ADMIN_VIEWS))
        roles["school_admin"] = school

        football, _ = Sport.objects.get_or_create(name="Football", defaults={"code": "FB"})
        cricket, _ = Sport.objects.get_or_create(name="Cricket", defaults={"code": "CR"})
        skating, _ = Sport.objects.get_or_create(name="Skating", defaults={"code": "SK"})
        loc, _ = TrainingLocation.objects.get_or_create(name="Main Ground")
        stype, _ = SessionType.objects.get_or_create(name="Regular")

        def user(username, role, pwd="Jetz@123"):
            u, created = User.objects.get_or_create(username=username, defaults={"role": roles[role], "is_active": True})
            if created:
                u.set_password(pwd)
                u.save()
            return u

        admin = user("admin", "super_admin")
        manager = user("manager", "academy_admin")

        from apps.academy.models import School
        main_school, _ = School.objects.get_or_create(name="HARI Sports Academy", defaults={"code": "HARI-MAIN"})

        coach, _ = Coach.objects.get_or_create(trainer_id="TRN-001", defaults={
            "first_name": "Demo", "last_name": "Coach", "phone": "9000000001", "weekly_off_days": [5, 6],
            "school": main_school})
        for s in (football, cricket, skating):
            CoachSport.objects.get_or_create(coach=coach, sport=s)
        coach_user = user("coach1", "coach")
        coach_user.coach_profile = coach
        coach_user.save()

        parent, _ = Parent.objects.get_or_create(first_name="Demo", last_name="Parent",
                                                 defaults={"relationship": "father", "mobile": "9000000002",
                                                           "whatsapp": "9000000002"})
        parent_user = user("parent1", "parent")
        parent_user.parent_profile = parent
        parent_user.save()

        plan, _ = FeePlan.objects.get_or_create(name="Standard Monthly", cycle="monthly",
                                                defaults={"amount": 2000, "sport": football})

        batch, _ = Batch.objects.get_or_create(name="FB-U12-Morning", sport=football,
                                               defaults={"coach": coach, "start_time": time(7, 0),
                                                         "end_time": time(8, 30), "location": loc,
                                                         "training_days": [0, 2, 4], "capacity": 30,
                                                         "school": main_school})
        student, _ = Student.objects.get_or_create(student_id="JETZ-0001", defaults={
            "first_name": "Demo", "last_name": "Student", "sport": football,
            "enrollment_date": date.today(), "status": "active", "phone": "9000000003",
            "school": main_school})
        BatchStudent.objects.get_or_create(batch=batch, student=student)
        StudentParent.objects.get_or_create(student=student, parent=parent, defaults={"is_primary": True})
        tomorrow = date.today() + timedelta(days=1)
        if tomorrow.weekday() in (coach.weekly_off_days or []):
            tomorrow += timedelta(days=2)
        Session.objects.get_or_create(batch=batch, sport=football, coach=coach, date=tomorrow,
                                      start_time=batch.start_time, end_time=batch.end_time,
                                      defaults={"session_type": stype, "location": loc})
        StudentFee.objects.get_or_create(student=student, fee_plan=plan, period_label="2026-10",
                                         defaults={"amount": plan.amount, "due_date": date.today(), "batch": batch})

        # Fee cycles beyond monthly, so the client can see quarterly / half-yearly plans.
        FeePlan.objects.get_or_create(name="Standard Quarterly", cycle="quarterly",
                                      defaults={"amount": 5400, "sport": football})
        FeePlan.objects.get_or_create(name="Standard Half-Yearly", cycle="half_yearly",
                                      defaults={"amount": 10000, "sport": football})

        # Trainer salary: per-session rate so completed sessions become payable.
        from apps.trainer_fees.models import TrainerFeeRule
        TrainerFeeRule.objects.get_or_create(
            trainer=coach, rate_per_session=500, effective_from=date.today() - timedelta(days=90),
            defaults={"is_active": True})

        # Make-up demo: a missed weekday session (absent) plus a linked make-up session.
        from apps.attendance.models import AttendanceRecord
        from apps.sessions.models import SessionStudent
        missed_date = date.today() - timedelta(days=7)
        if missed_date.weekday() in (coach.weekly_off_days or []):
            missed_date -= timedelta(days=1)
        missed, _ = Session.objects.get_or_create(
            batch=batch, sport=football, coach=coach, date=missed_date,
            start_time=batch.start_time, end_time=batch.end_time,
            defaults={"session_type": stype, "location": loc, "status": "completed"})
        SessionStudent.objects.get_or_create(session=missed, student=student)
        AttendanceRecord.objects.get_or_create(session=missed, student=student, defaults={"status": "absent"})
        makeup_start = time(9, 0)
        makeup, _ = Session.objects.get_or_create(
            batch=batch, sport=football, coach=coach, date=tomorrow,
            start_time=makeup_start, end_time=time(10, 0),
            defaults={"session_type": stype, "location": loc, "is_makeup": True,
                      "makeup_for": missed, "status": "completed"})
        SessionStudent.objects.get_or_create(session=makeup, student=student)

        WhatsAppTemplate.objects.get_or_create(name="fee_reminder", defaults={
            "category": "fee_reminder", "body": "Dear parent, fee of {{amount}} for {{student}} is due on {{due_date}}."})
        WhatsAppTemplate.objects.get_or_create(name="payment_confirmation", defaults={
            "category": "payment_confirmation", "body": "Payment of {{amount}} received for {{student}}. Receipt: {{receipt}}."})
        self.stdout.write(self.style.SUCCESS("Demo seed complete: admin/manager/coach1/parent1 (password Jetz@123)"))
