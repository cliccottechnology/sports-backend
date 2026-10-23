"""Trainer salary: one configured method per trainer per effective period.

Two calculation methods are supported side by side:

- Session-based: Eligible Completed Sessions x Session Rate = Payable. Rules
  (`TrainerFeeRule`) carry the per-session rate / fixed salary.
- Student-based: a snapshot of eligible students in a period paid either by a
  flat per-student rate or by configured student-count bands. Configured on
  `TrainerSalaryConfig` (with optional `StudentSalaryBand` rows).

Both roll up into the same `TrainerPayable` ledger so verification, payments,
adjustments and reporting are identical regardless of the method.
"""
from datetime import date

from django.db import models


class TrainerFeeRule(models.Model):
    PAYMENT_MODELS = [("per_session", "Per session"), ("weekly", "Weekly salary"), ("monthly", "Monthly salary")]
    trainer = models.ForeignKey("coaches.Coach", on_delete=models.CASCADE, related_name="fee_rules")
    sport = models.ForeignKey("academy.Sport", null=True, blank=True, on_delete=models.SET_NULL)
    batch = models.ForeignKey("batches.Batch", null=True, blank=True, on_delete=models.SET_NULL)
    session_type = models.ForeignKey("academy.SessionType", null=True, blank=True, on_delete=models.SET_NULL)
    payment_model = models.CharField(max_length=20, choices=PAYMENT_MODELS, default="per_session")
    rate_per_session = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # per-session model
    fixed_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # weekly/monthly salary
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["trainer", "is_active"])]

    def amount_for_session(self):
        return self.rate_per_session if self.payment_model == "per_session" else self.fixed_amount


class TrainerSalaryConfig(models.Model):
    """The *chosen* salary method for one trainer over an effective period.

    Exactly one method may be active for a trainer at any given date. A method
    change is a new config row with a later `effective_from` (and the previous
    row closed via `effective_to`); approved/paid payables are never rewritten.
    """
    METHOD_SESSION = "session_based"
    METHOD_STUDENT = "student_based"
    METHOD_WEEKLY = "weekly"
    METHOD_MONTHLY = "monthly"
    METHODS = [
        ("session_based", "Session-based"),
        ("student_based", "Student-based"),
        ("weekly", "Fixed weekly salary"),
        ("monthly", "Fixed monthly salary"),
    ]
    STUDENT_MODES = [("per_student", "Per-student rate"), ("banded", "Student-count bands")]
    ABOVE_TOP_BAND = [("cap", "Cap at top band"), ("per_student", "Charge per-student rate"),
                      ("reject", "Reject generation")]

    trainer = models.ForeignKey("coaches.Coach", on_delete=models.CASCADE, related_name="salary_configs")
    sport = models.ForeignKey("academy.Sport", null=True, blank=True, on_delete=models.SET_NULL)
    method = models.CharField(max_length=20, choices=METHODS, default="session_based")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Student-based configuration.
    student_mode = models.CharField(max_length=20, choices=STUDENT_MODES, default="per_student")
    student_rate = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    above_top_band = models.CharField(max_length=20, choices=ABOVE_TOP_BAND, default="cap")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["trainer_id", "-effective_from"]
        indexes = [
            models.Index(fields=["trainer", "is_active"]),
            models.Index(fields=["trainer", "method", "effective_from"]),
        ]

    def is_effective_on(self, on_date):
        return (self.effective_from <= on_date
                and (self.effective_to is None or self.effective_to >= on_date))

    def overlaps(self, other):
        """True when [from,to] date ranges intersect (open end = infinity)."""
        a_end = self.effective_to or date.max
        b_end = other.effective_to or date.max
        return self.effective_from <= b_end and other.effective_from <= a_end

    def __str__(self):
        return f"{self.trainer_id} — {self.get_method_display()} from {self.effective_from}"


class StudentSalaryBand(models.Model):
    """A student-count band: [min_students, max_students] -> amount.

    `max_students=None` marks the top, unbounded band. Bands must not overlap;
    counts above the highest finite band follow `TrainerSalaryConfig.above_top_band`.
    """
    config = models.ForeignKey(TrainerSalaryConfig, on_delete=models.CASCADE, related_name="bands")
    min_students = models.PositiveIntegerField()
    max_students = models.PositiveIntegerField(null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        ordering = ["min_students"]
        indexes = [models.Index(fields=["config", "min_students"])]

    def covers(self, count):
        return count >= self.min_students and (self.max_students is None or count <= self.max_students)

    def __str__(self):
        top = self.max_students if self.max_students is not None else "+"
        return f"{self.min_students}-{top}: {self.amount}"


class TrainerPayable(models.Model):
    STATUS = [("draft", "Draft"), ("pending", "Pending"), ("verified", "Verified"),
              ("paid", "Paid"), ("cancelled", "Cancelled")]
    KIND = [("session", "Per session"), ("weekly_salary", "Weekly salary"),
            ("monthly_salary", "Monthly salary"), ("student_salary", "Student-based salary")]
    trainer = models.ForeignKey("coaches.Coach", on_delete=models.CASCADE, related_name="payables")
    # Per-session payables link exactly one session; salary payables span a period instead.
    session = models.OneToOneField("training_sessions.Session", null=True, blank=True,
                                   on_delete=models.CASCADE, related_name="trainer_payable")
    kind = models.CharField(max_length=20, choices=KIND, default="session")
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    # Student-based payables snapshot the eligible count at generation time so
    # later roster changes never rewrite history.
    student_count = models.PositiveIntegerField(null=True, blank=True)
    rule = models.ForeignKey(TrainerFeeRule, null=True, on_delete=models.SET_NULL)
    config = models.ForeignKey(TrainerSalaryConfig, null=True, blank=True, on_delete=models.SET_NULL)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS, default="pending", db_index=True)
    verified_by = models.ForeignKey("accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="verified_payables")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["trainer", "status"]),
            models.Index(fields=["trainer", "kind", "period_start"]),
        ]
        constraints = [
            models.UniqueConstraint(fields=["trainer", "kind", "period_start"],
                                    name="uniq_trainer_period_payable"),
        ]


class TrainerPayment(models.Model):
    METHOD_CHOICES = [("cash", "Cash"), ("upi", "UPI"), ("bank_transfer", "Bank transfer"), ("other", "Other")]
    trainer = models.ForeignKey("coaches.Coach", on_delete=models.CASCADE, related_name="trainer_payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default="cash")
    reference = models.CharField(max_length=100, unique=True)
    paid_on = models.DateField()
    paid_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class TrainerFeeAdjustment(models.Model):
    trainer = models.ForeignKey("coaches.Coach", on_delete=models.CASCADE, related_name="fee_adjustments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()
    approved_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
