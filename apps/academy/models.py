"""Academy master data: schools, sports, categories, locations, session types, settings."""
from django.db import models


class School(models.Model):
    """One onboarded school/academy. All operational data is scoped to a school;
    users with a school see only their school's data (super admins see all)."""
    name = models.CharField(max_length=150, unique=True)
    code = models.CharField(max_length=20, unique=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Sport(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class SportCategory(models.Model):
    sport = models.ForeignKey(Sport, on_delete=models.CASCADE, related_name="categories")
    name = models.CharField(max_length=100)  # e.g. U-10, U-14
    min_age = models.PositiveIntegerField(null=True, blank=True)
    max_age = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("sport", "name")


class Branch(models.Model):
    """A physical site/branch inside an academy. Operational rows may point at
    a branch; a user scoped to a school sees all of that school's branches."""
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("school", "name")
        ordering = ["name"]

    def __str__(self):
        return f"{self.school.name} · {self.name}"


class TrainingLocation(models.Model):
    """Training venues are academy-owned, so the same venue name may exist in
    different academies — uniqueness is per school, not global."""
    name = models.CharField(max_length=150)
    school = models.ForeignKey(School, null=True, blank=True, on_delete=models.CASCADE,
                               related_name="locations")
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="locations")
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("school", "name")
        ordering = ["name"]


class SessionType(models.Model):
    name = models.CharField(max_length=100, unique=True)  # Regular, Trial, Camp, Makeup
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)


class AcademySetting(models.Model):
    """Key/value configuration. `school=NULL` rows are platform-wide defaults
    (the historical single-academy data); a school-specific row of the same key
    overrides the default for that academy only."""
    key = models.CharField(max_length=100)
    school = models.ForeignKey(School, null=True, blank=True, on_delete=models.CASCADE,
                               related_name="settings")
    value = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(fields=["school", "key"], name="academysetting_per_school"),
            models.UniqueConstraint(fields=["key"], name="academysetting_global_key",
                                    condition=models.Q(school__isnull=True)),
        ]

    def __str__(self):
        return self.key if self.school_id is None else f"{self.school.name} · {self.key}"

    @property
    def scope(self):
        return self.school.name if self.school_id else "Platform"


class AcademyModule(models.Model):
    """Per-academy module enablement. A missing row means "enabled" so existing
    academies keep every feature until the Super Admin switches one off."""
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="module_flags")
    module = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("school", "module")
        ordering = ["module"]

    def __str__(self):
        state = "on" if self.enabled else "off"
        return f"{self.school.name} · {self.module} ({state})"


def get_setting(key, school_id=None, default=None):
    """Effective value for a key: academy override first, then platform default."""
    if school_id is not None:
        row = AcademySetting.objects.filter(school_id=school_id, key=key).first()
        if row is not None:
            return row.value
    row = AcademySetting.objects.filter(school__isnull=True, key=key).first()
    return default if row is None else row.value


class AcademyAchievement(models.Model):
    """Institute achievements showcase (tournaments, milestones, honours)."""
    title = models.CharField(max_length=200)
    date = models.DateField()
    sport = models.ForeignKey(Sport, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="achievements")
    description = models.TextField(blank=True)
    photo = models.ImageField(upload_to="academy/achievements/%Y/%m/", null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.title} ({self.date})"
