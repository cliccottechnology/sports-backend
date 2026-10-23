from django.db import models

MSG_STATUS = [("queued", "Queued"), ("sent", "Sent"), ("delivered", "Delivered"),
              ("read", "Read"), ("failed", "Failed")]


class WhatsAppTemplate(models.Model):
    name = models.CharField(max_length=150, unique=True)  # must match provider-approved template
    category = models.CharField(max_length=50)  # fee_reminder, payment_confirmation, session, general
    language = models.CharField(max_length=10, default="en")
    body = models.TextField()
    is_active = models.BooleanField(default=True)


class WhatsAppMessage(models.Model):
    template = models.ForeignKey(WhatsAppTemplate, null=True, on_delete=models.SET_NULL)
    to_number = models.CharField(max_length=20)
    body = models.TextField()
    provider = models.CharField(max_length=30, default="mock")
    provider_message_id = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=MSG_STATUS, default="queued", db_index=True)
    failure_info = models.TextField(blank=True)
    related_student = models.ForeignKey("students.Student", null=True, blank=True, on_delete=models.SET_NULL)
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
