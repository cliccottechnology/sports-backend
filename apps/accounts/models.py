"""Accounts: custom user, roles, granular permissions."""
from django.contrib.auth.models import AbstractUser
from django.db import models

ROLE_CHOICES = [
    ("super_admin", "Super Admin"),
    ("academy_admin", "Academy Management/Admin"),
    ("coach", "Coach/Trainer"),
    ("parent", "Parent/Student"),
]


class Permission(models.Model):
    code = models.CharField(max_length=100, unique=True)  # e.g. fees.manage
    name = models.CharField(max_length=200)
    module = models.CharField(max_length=100)

    def __str__(self):
        return self.code


class Role(models.Model):
    # System roles: super_admin, academy_admin, coach, parent.
    # Super admins may also create per-admin custom roles (e.g. admin_<username>)
    # carrying that admin's individual permission set.
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    permissions = models.ManyToManyField(Permission, through="RolePermission", related_name="roles")

    def __str__(self):
        return self.name

    @property
    def is_system(self) -> bool:
        return self.name in ("super_admin", "academy_admin", "coach", "parent")


class RolePermission(models.Model):
    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        unique_together = ("role", "permission")


class User(AbstractUser):
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="users")
    school = models.ForeignKey("academy.School", null=True, blank=True, on_delete=models.SET_NULL, related_name="users")
    phone = models.CharField(max_length=20, blank=True)
    is_archived = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    coach_profile = models.OneToOneField("coaches.Coach", null=True, blank=True, on_delete=models.SET_NULL, related_name="login_account")
    parent_profile = models.OneToOneField("parents.Parent", null=True, blank=True, on_delete=models.SET_NULL, related_name="login_account")

    def perm_codes(self):
        # The super_admin role is structurally omnipotent (like is_superuser):
        # stripping rows off that role can never lock out a Super Admin.
        from apps.accounts.rbac_catalog import ALL_CODES, LEGACY_MAP
        if self.is_superuser or (self.role and self.role.name == "super_admin"):
            return [c for c, _, _ in ALL_CODES]
        if not self.role:
            return []
        held = set(self.role.permissions.values_list("code", flat=True))
        expanded = set(held)
        for code in held:
            if code.endswith(".manage"):
                module = code[: -len(".manage")]
                expanded.update(c for c, _, _ in ALL_CODES if c.startswith(module + "."))
            expanded.update(LEGACY_MAP.get(code, []))
        return sorted(expanded)

    def has_perm_code(self, code: str) -> bool:
        if self.is_superuser or (self.role and self.role.name == "super_admin"):
            return True
        if not self.role:
            return False
        return code in self.perm_codes()
