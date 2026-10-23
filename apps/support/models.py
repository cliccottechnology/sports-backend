from django.db import models


class SupportTicket(models.Model):
    CATEGORY = [
        ("technical", "Technical"),
        ("billing", "Billing & Fees"),
        ("account", "Account & Access"),
        ("academic", "Academic / Academy"),
        ("other", "Other"),
    ]
    PRIORITY = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]
    STATUS = [
        ("open", "Open"),
        ("in_progress", "In progress"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    ]

    subject = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=CATEGORY, default="other")
    priority = models.CharField(max_length=10, choices=PRIORITY, default="medium")
    status = models.CharField(max_length=20, choices=STATUS, default="open")
    created_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="support_tickets")
    assigned_to = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="assigned_support_tickets")
    school = models.ForeignKey("academy.School", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="support_tickets")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"#{self.pk} {self.subject}"


class TicketReply(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="replies")
    author = models.ForeignKey("accounts.User", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="support_replies")
    body = models.TextField()
    is_internal = models.BooleanField(default=False)  # staff-only note, hidden from the raiser
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"reply#{self.pk} on ticket {self.ticket_id}"


class TicketAttachment(models.Model):
    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="attachments")
    reply = models.ForeignKey(TicketReply, null=True, blank=True,
                              on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="support/%Y/%m/")
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.file.name if self.file else f"attachment#{self.pk}"
