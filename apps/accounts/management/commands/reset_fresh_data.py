"""Reset database to a fresh state with clean Super Admin and Admin accounts.

Usage:
    python manage.py reset_fresh_data [--password Jetz@123]
"""
import os
import shutil
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Wipe all transactional data (students, parents, coaches, batches, attendance, fees) and ensure clean Super Admin and Admin."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default="Jetz@123",
            help="Default password for super_admin and admin accounts (default: Jetz@123)",
        )
        parser.add_argument(
            "--purge-aws-faces",
            action="store_true",
            default=True,
            help="Purge all faces in AWS Rekognition collection",
        )

    def handle(self, *args, **options):
        pwd = options["password"]
        self.stdout.write(self.style.WARNING("Starting database reset to fresh state..."))

        # 1. AWS Rekognition collection purge
        if options.get("purge_aws_faces"):
            self._purge_aws_rekognition()

        # 2. Database cleanup within transaction
        with transaction.atomic():
            self._wipe_transactional_data()
            self._ensure_admin_accounts(pwd)
            self._ensure_master_data()

        # 3. Clean media storage
        self._clean_media_directories()

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("Database has been reset to a fresh state successfully!"))
        self.stdout.write(self.style.SUCCESS(f"Super Admin: username='admin'         password='{pwd}'"))
        self.stdout.write(self.style.SUCCESS(f"Super Admin: username='cliccot_admin'  password='{pwd}'"))
        self.stdout.write(self.style.SUCCESS(f"Academy Admin: username='manager'     password='{pwd}'"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

    def _purge_aws_rekognition(self):
        try:
            from faceauth.providers import get_provider
            p = get_provider()
            if getattr(p, "name", "") == "aws":
                client = p._client()
                collections = ["cliccot-1", "cliccot-faces-1", "cliccot-students-1", "jetz-students-1"]
                for col in collections:
                    try:
                        faces = client.list_faces(CollectionId=col).get("Faces", [])
                        face_ids = [f["FaceId"] for f in faces]
                        if face_ids:
                            # AWS Rekognition delete_faces takes max 100 per call
                            for i in range(0, len(face_ids), 100):
                                chunk = face_ids[i:i + 100]
                                client.delete_faces(CollectionId=col, FaceIds=chunk)
                            self.stdout.write(f"Purged {len(face_ids)} faces from AWS collection {col}")
                    except Exception as e:
                        # Collection might not exist or error
                        pass
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"Note: AWS Rekognition purge skipped: {exc}"))

    def _wipe_transactional_data(self):
        # Import models
        from apps.accounts.models import User
        from apps.students.models import Student, StudentDocument, StudentCertificate, StudentReferencePhoto, EnrollmentHistory
        from apps.parents.models import Parent, StudentParent
        from apps.coaches.models import (
            Coach, CoachSport, CoachAssignment, CoachAvailability,
            TrainerCheckIn, TrainerLeave, TrainerAttendanceCorrection, TrainerCertificate
        )
        from apps.batches.models import Batch, BatchStudent
        from apps.sessions.models import Session, SessionStudent
        from apps.attendance.models import AttendanceRecord, AttendanceCorrection, AttendancePhotoEvidence
        from apps.fees.models import FeePlan, StudentFee
        from apps.payments.models import Payment, Refund
        from apps.trainer_fees.models import (
            TrainerPayable, TrainerFeeRule, TrainerSalaryConfig, StudentSalaryBand
        )
        from apps.notifications.models import Notification
        from apps.audit.models import AuditLog
        from apps.academy.models import AcademyAchievement
        try:
            from apps.documents.models import Document
        except Exception:
            Document = None
        from faceauth.models import StudentFaceTemplate

        # Optional tokens
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
            BlacklistedToken.objects.all().delete()
            OutstandingToken.objects.all().delete()
            self.stdout.write("Cleared JWT outstanding/blacklisted tokens.")
        except Exception:
            pass

        # Attendance & Vision
        AttendanceCorrection.objects.all().delete()
        AttendanceRecord.objects.all().delete()
        AttendancePhotoEvidence.objects.all().delete()
        StudentFaceTemplate.objects.all().delete()
        self.stdout.write("Cleared attendance records, evidence, and face templates.")

        # Sessions & Batches
        SessionStudent.objects.all().delete()
        Session.objects.all().delete()
        BatchStudent.objects.all().delete()
        Batch.objects.all().delete()
        self.stdout.write("Cleared sessions and batches.")

        # Students & Parents
        StudentParent.objects.all().delete()
        StudentDocument.objects.all().delete()
        StudentCertificate.objects.all().delete()
        StudentReferencePhoto.objects.all().delete()
        EnrollmentHistory.objects.all().delete()
        Refund.objects.all().delete()
        Payment.objects.all().delete()
        StudentFee.objects.all().delete()
        FeePlan.objects.all().delete()
        self.stdout.write("Cleared student fees, payments, and fee plans.")

        # Trainer Fees & Attendance
        TrainerPayable.objects.all().delete()
        StudentSalaryBand.objects.all().delete()
        TrainerSalaryConfig.objects.all().delete()
        TrainerFeeRule.objects.all().delete()
        TrainerAttendanceCorrection.objects.all().delete()
        TrainerCheckIn.objects.all().delete()
        TrainerLeave.objects.all().delete()
        TrainerCertificate.objects.all().delete()
        CoachAssignment.objects.all().delete()
        CoachAvailability.objects.all().delete()
        CoachSport.objects.all().delete()
        self.stdout.write("Cleared trainer fees, check-ins, and coach assignments.")

        # Unlink coaches and parents from users before deleting them
        User.objects.all().update(coach_profile=None, parent_profile=None)

        # Delete students, parents, coaches, sports
        Student.objects.all().delete()
        Parent.objects.all().delete()
        Coach.objects.all().delete()
        from apps.academy.models import Sport
        Sport.objects.all().delete()
        self.stdout.write("Cleared students, parents, coaches, and sports.")

        # Miscellaneous
        Notification.objects.all().delete()
        AuditLog.objects.all().delete()
        if Document is not None:
            Document.objects.all().delete()
        AcademyAchievement.objects.all().delete()

        try:
            from apps.whatsapp.models import WhatsAppMessage
            WhatsAppMessage.objects.all().delete()
        except Exception:
            pass

        # Delete non-admin user accounts
        keep_usernames = ["admin", "cliccot_admin", "manager", "harish"]
        deleted_users, _ = User.objects.exclude(username__in=keep_usernames).delete()
        self.stdout.write(f"Removed {deleted_users} non-admin user accounts.")

    def _ensure_admin_accounts(self, pwd):
        from apps.accounts.models import Permission, Role, User
        from apps.accounts.rbac_catalog import ALL_CODES

        # 1. Ensure permissions
        for code, name, module in ALL_CODES:
            Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})

        # 2. Roles
        roles = {}
        for rname in ("super_admin", "academy_admin", "coach", "parent", "school_admin"):
            role, _ = Role.objects.get_or_create(name=rname, defaults={"description": rname})
            roles[rname] = role

        # Super admin gets all permissions
        roles["super_admin"].permissions.set(Permission.objects.all())

        # Academy admin gets all management permissions
        admin_perms = Permission.objects.exclude(code__startswith="system.")
        roles["academy_admin"].permissions.set(admin_perms)

        # 3. Super admin user: admin
        admin_user, _ = User.objects.get_or_create(
            username="admin",
            defaults={"is_active": True, "is_superuser": True, "is_staff": True}
        )
        admin_user.role = roles["super_admin"]
        admin_user.is_superuser = True
        admin_user.is_staff = True
        admin_user.is_active = True
        admin_user.set_password(pwd)
        admin_user.save()

        # 4. Super admin user: cliccot_admin
        cliccot_user, _ = User.objects.get_or_create(
            username="cliccot_admin",
            defaults={"is_active": True, "is_superuser": True, "is_staff": True}
        )
        cliccot_user.role = roles["super_admin"]
        cliccot_user.is_superuser = True
        cliccot_user.is_staff = True
        cliccot_user.is_active = True
        cliccot_user.set_password(pwd)
        cliccot_user.save()

        # 5. Academy admin user: manager
        manager_user, _ = User.objects.get_or_create(
            username="manager",
            defaults={"is_active": True, "is_superuser": False, "is_staff": False}
        )
        manager_user.role = roles["academy_admin"]
        manager_user.is_active = True
        manager_user.set_password(pwd)
        manager_user.save()

        self.stdout.write("Configured clean admin accounts with full permissions.")

    def _ensure_master_data(self):
        from apps.academy.models import School, SessionType, Sport, TrainingLocation
        from apps.whatsapp.models import WhatsAppTemplate

        # Ensure default academy school
        main_school, _ = School.objects.get_or_create(
            name="HARI Sports Academy",
            defaults={"code": "HARI-MAIN"}
        )

        # Ensure default location & session type
        TrainingLocation.objects.get_or_create(name="Main Ground")
        SessionType.objects.get_or_create(name="Regular")

        # Sports are a required FK on batches and sessions. The wipe above
        # removes them, so without this the Batches form can never submit.
        sports = {
            "Cricket": "CRK",
            "Football": "FBL",
            "Basketball": "BSK",
            "Badminton": "BDM",
            "Tennis": "TNS",
            "Volleyball": "VBL",
            "Athletics": "ATH",
            "Swimming": "SWM",
            "Hockey": "HKY",
            "Table Tennis": "TTN",
        }
        for name, code in sports.items():
            if Sport.objects.filter(name__iexact=name).exists():
                continue
            Sport.objects.get_or_create(code=code, defaults={"name": name})

        # WhatsApp templates
        WhatsAppTemplate.objects.get_or_create(
            name="fee_reminder",
            defaults={
                "category": "fee_reminder",
                "body": "Dear parent, fee of {{amount}} for {{student}} is due on {{due_date}}."
            }
        )
        WhatsAppTemplate.objects.get_or_create(
            name="payment_confirmation",
            defaults={
                "category": "payment_confirmation",
                "body": "Payment of {{amount}} received for {{student}}. Receipt: {{receipt}}."
            }
        )
        self.stdout.write("Ensured master data: School, Sports, Training Location, Session Types, Templates.")

    def _clean_media_directories(self):
        media_root = getattr(settings, "MEDIA_ROOT", None)
        if not media_root or not os.path.exists(media_root):
            return

        cleanup_subdirs = [
            os.path.join(media_root, "attendance"),
            os.path.join(media_root, "students"),
            os.path.join(media_root, "coaches"),
            os.path.join(media_root, "academy", "achievements"),
        ]
        for d in cleanup_subdirs:
            if os.path.exists(d):
                for item in os.listdir(d):
                    item_path = os.path.join(d, item)
                    try:
                        if os.path.isfile(item_path) or os.path.islink(item_path):
                            os.unlink(item_path)
                        elif os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                    except Exception as e:
                        pass
                self.stdout.write(f"Cleaned media directory: {d}")
