from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class Batch(models.Model):
    name = models.CharField(max_length=150)
    school = models.ForeignKey("academy.School", null=True, blank=True, on_delete=models.PROTECT, related_name="batches")
    sport = models.ForeignKey("academy.Sport", on_delete=models.PROTECT, related_name="batches")
    category = models.ForeignKey("academy.SportCategory", null=True, blank=True, on_delete=models.SET_NULL)
    coach = models.ForeignKey("coaches.Coach", null=True, on_delete=models.SET_NULL, related_name="batches")
    capacity = models.PositiveIntegerField(null=True, blank=True)  # configurable; enforced when set
    training_days = models.JSONField(default=list, blank=True)  # [0..6]
    start_time = models.TimeField()
    end_time = models.TimeField()
    location = models.ForeignKey("academy.TrainingLocation", null=True, on_delete=models.SET_NULL)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Same batch name may repeat across schools, never twice in one school.
        # (NULL schools never collide in PostgreSQL, so existing rows migrate cleanly.)
        unique_together = ("school", "name", "sport")

    def clean(self):
        if self.sport and not self.sport.is_active and not self.pk:
            raise ValidationError("Inactive sports cannot be assigned to new batches.")


class BatchStudent(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="rosters")
    student = models.ForeignKey("students.Student", on_delete=models.CASCADE, related_name="batch_links")
    joined_on = models.DateField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("batch", "student")

    def clean(self):
        if self.batch.capacity:
            active = BatchStudent.objects.filter(batch=self.batch, is_active=True).exclude(pk=self.pk).count()
            if self.is_active and active >= self.batch.capacity:
                raise ValidationError("Batch capacity reached.")
