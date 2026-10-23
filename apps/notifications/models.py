from django.db import models


class NotificationTemplate(models.Model):
    name = models.CharField(max_length=150, unique=True)
    channel = models.CharField(max_length=20, default="in_app")  # in_app/email/sms/push
    subject = models.CharField(max_length=200, blank=True)
    body = models.TextField()
    is_active = models.BooleanField(default=True)


class Notification(models.Model):
    CHANNEL = [("in_app", "In-app inbox"), ("push", "Push")]
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=200)
    message = models.TextField()
    channel = models.CharField(max_length=10, choices=CHANNEL, default="in_app")
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
