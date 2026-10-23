from django.db import models


class Parent(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    relationship = models.CharField(max_length=50)  # father/mother/guardian
    mobile = models.CharField(max_length=20)
    whatsapp = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    emergency_contact = models.CharField(max_length=100, blank=True)
    communication_preference = models.CharField(max_length=20, default="whatsapp")  # whatsapp/sms/email
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.relationship})".strip()


class StudentParent(models.Model):
    student = models.ForeignKey("students.Student", on_delete=models.CASCADE, related_name="parent_links")
    parent = models.ForeignKey(Parent, on_delete=models.CASCADE, related_name="student_links")
    is_primary = models.BooleanField(default=False)

    class Meta:
        unique_together = ("student", "parent")
