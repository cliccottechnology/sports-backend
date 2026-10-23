from django.db import models


class CustomizationRequest(models.Model):
    CATEGORY = [
        ("new_feature", "New Feature"),
        ("ui_ux", "UI/UX Improvement"),
        ("report", "Report or Analytics"),
        ("workflow", "Workflow Change"),
        ("bug_fix", "Bug Fix"),
        ("access_permission", "Access or Permission Request"),
        ("notification_integration", "Notification or Integration"),
        ("other", "Other"),
    ]
    PRIORITY = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]
    STATUS = [
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("under_review", "Under Review"),
        ("clarification_required", "Clarification Required"),
        ("approved", "Approved"),
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("ready_for_review", "Ready for Review"),
        ("completed", "Completed"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]

    # Open statuses that a duplicate-notification guard treats as "already pending".
    OPEN_STATUSES = ["submitted", "under_review", "clarification_required",
                     "approved", "assigned", "in_progress", "ready_for_review"]

    request_id = models.CharField(max_length=20, unique=True, blank=True)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=30, choices=CATEGORY, default="new_feature")
    description = models.TextField()
    reason = models.TextField(blank=True)
    expected_outcome = models.TextField(blank=True)
    module_page = models.CharField(max_length=200, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITY, default="medium")
    status = models.CharField(max_length=30, choices=STATUS, default="submitted")

    created_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="customization_requests")
    school = models.ForeignKey("academy.School", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="customization_requests")
    assigned_to = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="assigned_customization_requests")
    assigned_team = models.CharField(max_length=150, blank=True)

    decision_reason = models.TextField(blank=True)  # rejection / approval note
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.request_id or 'CR'} {self.title}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if not self.request_id:
            self.request_id = f"HARI-CR-{self.pk:04d}"
            super().save(update_fields=["request_id"])


class RequestStatusHistory(models.Model):
    request = models.ForeignKey(CustomizationRequest, on_delete=models.CASCADE, related_name="history")
    from_status = models.CharField(max_length=30, blank=True)
    to_status = models.CharField(max_length=30)
    changed_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class RequestComment(models.Model):
    KIND = [
        ("comment", "Comment"),
        ("clarification", "Clarification Request"),
        ("reply", "Reply"),
        ("decision", "Decision"),
        ("progress", "Progress Note"),
    ]
    request = models.ForeignKey(CustomizationRequest, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    body = models.TextField()
    kind = models.CharField(max_length=20, choices=KIND, default="comment")
    is_internal = models.BooleanField(default=False)  # staff-only note, hidden from the requester
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class RequestAttachment(models.Model):
    request = models.ForeignKey(CustomizationRequest, on_delete=models.CASCADE, related_name="attachments")
    comment = models.ForeignKey(RequestComment, null=True, blank=True,
                                on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="customization/%Y/%m/")
    uploaded_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.file.name if self.file else f"attachment#{self.pk}"
