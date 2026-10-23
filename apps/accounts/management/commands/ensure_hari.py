"""Ensure the HARI Super Admin account exists and is fully protected.

Idempotent and non-destructive: never touches other accounts, never deletes
data. If `hari` already exists it is repaired into the super_admin role
(without resetting a working password); otherwise it is created.

Usage:
    python manage.py ensure_hari [--password ...] [--force-password]

Without --password a secure random one is generated and printed ONCE.
"""
import secrets
import string

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Ensure the HARI super admin account (role + superuser flag)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="", help="Password to set (min 8 chars).")
        parser.add_argument("--force-password", action="store_true",
                            help="Reset the password even if the account already works.")

    def handle(self, *args, **options):
        from apps.accounts.models import Role, User
        role, _ = Role.objects.get_or_create(
            name="super_admin", defaults={"description": "Super Admin"})
        pwd = options["password"]
        if pwd and len(pwd) < 8:
            raise ValueError("Password must be at least 8 characters.")
        generated = False
        if not pwd:
            alphabet = string.ascii_letters + string.digits + "!@#$%"
            pwd = "".join(secrets.choice(alphabet) for _ in range(16))
            generated = True
        user, created = User.objects.get_or_create(
            username="hari",
            defaults={"role": role, "is_active": True, "is_superuser": True,
                      "is_staff": True, "must_change_password": True},
        )
        repaired = []
        if user.role_id != role.id:
            user.role = role
            repaired.append("role->super_admin")
        for flag in ("is_active", "is_superuser", "is_staff"):
            if not getattr(user, flag):
                setattr(user, flag, True)
                repaired.append(flag)
        show_pwd = created or options["force_password"]
        if created or options["force_password"]:
            user.set_password(pwd)
            user.must_change_password = True
            user.save()
            status = "created" if created else "password reset"
        elif repaired:
            user.save()
            status = f"repaired ({', '.join(repaired)})"
        else:
            status = "already correct — password untouched"
        self.stdout.write(self.style.SUCCESS(f"HARI super admin {status}."))
        if show_pwd:
            self.stdout.write(self.style.WARNING(
                f"Username: hari | Password: {pwd} | Change it after first login."))
