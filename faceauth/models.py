"""Face-auth models for JETZ Sports Academy.

This app adapts the face-matching engine (AWS Rekognition) to the academy's
existing student / attendance domain. It stores only the *pointer* to a face
enrolled in a provider collection (`face_id`) plus the consent record — no
biometric pixels are kept here.

Attendance itself is still written through the normal attendance API
(`/api/attendance/photo_analyze/` -> coach review -> `/api/attendance/bulk_mark/`);
this app only produces the per-student suggestions.
"""
from django.db import models


class StudentFaceTemplate(models.Model):
    """One consented reference photo indexed in the provider's face collection.

    A student may have several active templates (one per reference photo /
    angle). The row is a mapping only: `face_id` is the opaque id the provider
    returned for that photo. When the source photo is deleted the row (and,
    during the next sync, the provider face) goes with it.
    """

    school = models.ForeignKey(
        "academy.School", null=True, blank=True, on_delete=models.CASCADE,
        related_name="student_face_templates",
    )
    student = models.ForeignKey(
        "students.Student", on_delete=models.CASCADE,
        related_name="face_templates",
    )
    source_photo = models.ForeignKey(
        "students.StudentReferencePhoto", null=True, blank=True,
        on_delete=models.CASCADE, related_name="face_templates",
    )
    # Opaque identifier returned by the provider (AWS Rekognition FaceId).
    face_id = models.CharField(max_length=128, unique=True)
    provider = models.CharField(max_length=50, default="aws")
    consent_given = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["student_id", "created_at"]
        indexes = [
            models.Index(fields=["school", "student"]),
            models.Index(fields=["student", "provider"]),
        ]

    def __str__(self):
        return f"StudentFaceTemplate(student={self.student_id} face={self.face_id})"
