from django.core.exceptions import ValidationError
from django.db import models

WEEKDAYS = [(i, d) for i, d in enumerate(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])]


class Coach(models.Model):
    trainer_id = models.CharField(max_length=30, unique=True)
    school = models.ForeignKey("academy.School", null=True, blank=True, on_delete=models.PROTECT, related_name="coaches")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    photo = models.ImageField(upload_to="coaches/%Y/%m/", null=True, blank=True)
    is_active = models.BooleanField(default=True)
    weekly_off_days = models.JSONField(default=list, blank=True)  # e.g. [5, 6]; exactly two expected
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        if self.weekly_off_days and len(self.weekly_off_days) != 2:
            raise ValidationError("Coach must have exactly two weekly off-days.")

    def __str__(self):
        return f"{self.trainer_id} — {self.first_name} {self.last_name}".strip()


class CoachSport(models.Model):
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="sport_links")
    sport = models.ForeignKey("academy.Sport", on_delete=models.CASCADE)

    class Meta:
        unique_together = ("coach", "sport")


class CoachAvailability(models.Model):
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="availability")
    weekday = models.IntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_available = models.BooleanField(default=True)

    class Meta:
        unique_together = ("coach", "weekday", "start_time")


class CoachAssignment(models.Model):
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="assignments")
    batch = models.ForeignKey("batches.Batch", on_delete=models.CASCADE, related_name="coach_assignments")
    assigned_from = models.DateField()
    assigned_to = models.DateField(null=True, blank=True)


class TrainerCheckIn(models.Model):
    """Trainer's own attendance: one check-in per (session, coach), marked manually."""
    STATUS_CHOICES = [("present", "Present"), ("late", "Late"),
                      ("absent", "Absent"), ("leave", "Leave")]

    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="checkins")
    session = models.ForeignKey("training_sessions.Session", on_delete=models.CASCADE, related_name="trainer_checkins")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="present")
    notes = models.CharField(max_length=255, blank=True)
    marked_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    marked_at = models.DateTimeField(auto_now_add=True)
    # True when the system recorded this (missed-deadline auto absence) rather
    # than a human. Never overwrites a human mark; a correction request can
    # later restore the trainer's real status with an audit trail.
    auto_marked = models.BooleanField(default=False)

    class Meta:
        unique_together = ("session", "coach")
        indexes = [models.Index(fields=["coach", "session"])]
    is_primary = models.BooleanField(default=True)


class TrainerLeave(models.Model):
    """Approved leave for a trainer, whole-day or tied to one session.

    The automatic absence sweep skips any session covered by an approved leave
    so an on-leave trainer is never recorded absent."""
    STATUS_CHOICES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]

    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="leaves")
    session = models.ForeignKey("training_sessions.Session", null=True, blank=True,
                                on_delete=models.CASCADE, related_name="trainer_leaves")
    date = models.DateField(db_index=True)
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    requested_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="requested_trainer_leaves")
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="reviewed_trainer_leaves")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["coach", "date", "status"])]

    @staticmethod
    def covers(coach_id, session):
        """True when an approved leave covers this coach + session/date."""
        from django.db.models import Q
        return TrainerLeave.objects.filter(
            coach_id=coach_id, status="approved").filter(
            Q(session_id=session.pk) | Q(session__isnull=True, date=session.date)).exists()


class TrainerAttendanceCorrection(models.Model):
    """Trainer's request to fix an automatically-marked absence.

    The original status is snapshotted and never silently overwritten: approval
    updates the check-in under an audit entry, keeps `original_status`, and
    records the reviewer, timestamp and decision note."""
    STATUS_CHOICES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]
    REQUESTABLE = [("present", "Present"), ("late", "Late"), ("leave", "Leave")]

    checkin = models.ForeignKey(TrainerCheckIn, on_delete=models.CASCADE, related_name="corrections")
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="attendance_corrections")
    session = models.ForeignKey("training_sessions.Session", null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="trainer_corrections")
    requested_status = models.CharField(max_length=20, choices=REQUESTABLE, default="present")
    original_status = models.CharField(max_length=20, blank=True)
    reason = models.TextField()
    evidence = models.FileField(upload_to="trainer/corrections/%Y/%m/", null=True, blank=True)
    evidence_note = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey("accounts.User", null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="reviewed_trainer_corrections")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]
        indexes = [models.Index(fields=["checkin", "status"]),
                   models.Index(fields=["coach", "status"])]


class TrainerAttendanceReminder(models.Model):
    """One reminder per (coach, session) so scheduled runs never double-notify."""
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="attendance_reminders")
    session = models.ForeignKey("training_sessions.Session", on_delete=models.CASCADE,
                                related_name="attendance_reminders")
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("coach", "session")


class TrainerCertificate(models.Model):
    """Institute certificate for a trainer (e.g. 3-month completion).

    The session count is snapshotted at issue time so later data changes
    never rewrite history. The PDF is generated on issue.
    """
    trainer = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="certificates")
    title = models.CharField(max_length=200, default="Certificate of Service Excellence")
    period_from = models.DateField()
    period_to = models.DateField()
    sessions_completed = models.PositiveIntegerField(default=0)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to="trainers/certificates/%Y/%m/", null=True, blank=True)
    issued_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-issued_at"]
        indexes = [models.Index(fields=["trainer", "issued_at"])]
