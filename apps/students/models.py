from django.core.exceptions import ValidationError
from django.db import models

STATUS_CHOICES = [
    ("active", "Active"), ("inactive", "Inactive"), ("pending", "Pending"),
    ("suspended", "Suspended"), ("completed", "Completed"), ("withdrawn", "Withdrawn"),
]


class Student(models.Model):
    student_id = models.CharField(max_length=30, unique=True)  # academy unique ID
    school = models.ForeignKey("academy.School", null=True, blank=True, on_delete=models.PROTECT, related_name="students")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    whatsapp = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    emergency_contact = models.CharField(max_length=100, blank=True)
    photo = models.ImageField(upload_to="students/photos/%Y/%m/", null=True, blank=True)
    sport = models.ForeignKey("academy.Sport", null=True, on_delete=models.SET_NULL, related_name="students")
    enrollment_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status"]), models.Index(fields=["sport"]), models.Index(fields=["student_id"])]

    def __str__(self):
        return f"{self.student_id} — {self.first_name} {self.last_name}".strip()


class StudentDocument(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=150)
    file = models.FileField(upload_to="students/%Y/%m/")
    file_type = models.CharField(max_length=20, blank=True)
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class EnrollmentHistory(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="enrollment_history")
    batch = models.ForeignKey("batches.Batch", null=True, on_delete=models.SET_NULL)
    fee_plan = models.ForeignKey("fees.FeePlan", null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=50)  # enrolled, transferred, withdrawn...
    changed_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)


class StudentReferencePhoto(models.Model):
    """Consented enrollment photos used by face-matching providers.

    A student may have MULTIPLE active reference photos — one per angle
    (front / left / right / other) — so AI attendance matching can detect
    athletes from different angles. Deactivating (instead of deleting)
    preserves audit history.
    """
    ANGLE_CHOICES = [
        ("front", "Front"), ("left", "Left side"),
        ("right", "Right side"), ("other", "Other angle"),
    ]
    MAX_ACTIVE_PER_STUDENT = 5

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="reference_photos")
    image = models.ImageField(upload_to="students/reference/%Y/%m/")
    angle = models.CharField(max_length=20, choices=ANGLE_CHOICES, default="front")
    consent_given = models.BooleanField(default=False)
    consented_by = models.CharField(max_length=200, blank=True)
    consented_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["student", "is_active"])]


class StudentCertificate(models.Model):
    """Institute certificate for a student's completed training milestone.

    Eligibility is computed from the sport enrollment date plus the
    academy-configured policy (duration, optional attendance/session
    thresholds). Snapshots (attendance %, sessions, milestone) freeze at
    issue so later data changes never rewrite history. The certificate
    number is unique per student + sport + milestone start; re-issue after
    an explicit delete, which stays audited.
    """
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="certificates")
    sport = models.ForeignKey("academy.Sport", null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="student_certificates")
    title = models.CharField(max_length=200, default="Certificate of Training Completion")
    period_from = models.DateField()
    period_to = models.DateField()
    milestone_date = models.DateField()
    attendance_pct = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    sessions_completed = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)
    certificate_no = models.CharField(max_length=50, unique=True, blank=True)
    file = models.FileField(upload_to="students/certificates/%Y/%m/", null=True, blank=True)
    issued_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_at"]
        unique_together = ("student", "sport", "period_from")
        indexes = [models.Index(fields=["student", "sport"])]

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.certificate_no:
            self.certificate_no = f"CERT-{self.issued_at.year if self.issued_at else ''}-{self.pk:05d}"
            super().save(update_fields=["certificate_no"])
