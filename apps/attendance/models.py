"""Session-based attendance. One record per (session, student)."""
from django.db import models

STATUS_CHOICES = [("present", "Present"), ("absent", "Absent"), ("late", "Late"),
                  ("leave", "Leave"), ("not_marked", "Not Marked")]


class AttendanceRecord(models.Model):
    session = models.ForeignKey("training_sessions.Session", on_delete=models.CASCADE, related_name="attendance")
    student = models.ForeignKey("students.Student", on_delete=models.CASCADE, related_name="attendance")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="not_marked")
    marked_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    marked_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    locked = models.BooleanField(default=False)  # locked once session completed & reviewed
    # When this mark is a make-up for a missed session, the original session is
    # recorded here. The original absence keeps its own row on its own session,
    # so the two are recorded separately and linked (never duplicated).
    makeup_for = models.ForeignKey("training_sessions.Session", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="makeup_attendance")

    class Meta:
        unique_together = ("session", "student")
        indexes = [models.Index(fields=["session", "status"]), models.Index(fields=["student"])]


class AttendanceCorrection(models.Model):
    record = models.ForeignKey(AttendanceRecord, on_delete=models.CASCADE, related_name="corrections")
    old_status = models.CharField(max_length=20)
    new_status = models.CharField(max_length=20)
    reason = models.TextField()
    corrected_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)


class AttendancePhotoEvidence(models.Model):
    """Group photo submitted for AI-assisted attendance (audit evidence).

    The image is kept for review/audit. Biometric templates are only derived
    from consented reference photos, never from this group photo.
    """
    session = models.ForeignKey("training_sessions.Session", on_delete=models.CASCADE, related_name="photo_evidence")
    image = models.ImageField(upload_to="attendance/%Y/%m/")
    provider = models.CharField(max_length=30, default="faceauth")
    persons_detected = models.IntegerField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    dhash = models.CharField(max_length=16, null=True, blank=True)  # perceptual hash for duplicate detection
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
