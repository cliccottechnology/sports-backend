"""Fee management. Amounts are DB-driven (FeePlan) — never hardcoded."""
from django.db import models
from django.db.models import Sum

CYCLE_CHOICES = [("monthly", "Monthly"), ("quarterly", "Quarterly"), ("half_yearly", "Half-Yearly")]
FEE_STATUS = [("upcoming", "Upcoming"), ("due", "Due"), ("pending", "Pending"),
              ("partially_paid", "Partially Paid"), ("paid", "Paid"),
              ("overdue", "Overdue"), ("cancelled", "Cancelled"), ("refunded", "Refunded")]


class FeePlan(models.Model):
    name = models.CharField(max_length=150)
    sport = models.ForeignKey("academy.Sport", null=True, blank=True, on_delete=models.SET_NULL, related_name="fee_plans")
    cycle = models.CharField(max_length=20, choices=CYCLE_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # configured by JETZ admin
    currency = models.CharField(max_length=10, default="INR")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.cycle})"


class StudentFee(models.Model):
    """A fee allocation to a student for a cycle period."""
    student = models.ForeignKey("students.Student", on_delete=models.CASCADE, related_name="fees")
    fee_plan = models.ForeignKey(FeePlan, on_delete=models.PROTECT, related_name="allocations")
    batch = models.ForeignKey("batches.Batch", null=True, blank=True, on_delete=models.SET_NULL)
    period_label = models.CharField(max_length=50)  # e.g. 2026-10
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    due_date = models.DateField()
    status = models.CharField(max_length=20, choices=FEE_STATUS, default="due", db_index=True)
    created_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("student", "fee_plan", "period_label")
        indexes = [models.Index(fields=["status", "due_date"])]

    @property
    def paid_total(self):
        return self.payments.filter(status="success").aggregate(t=Sum("amount"))["t"] or 0

    @property
    def balance(self):
        return float(self.amount - self.discount) - float(self.paid_total)

    def refresh_status(self):
        """Recompute paid/partially_paid from actual successful payments.
        Returns the new status. Never touches cancelled/refunded, and never
        marks anything paid without real money behind it."""
        if self.status in ("cancelled", "refunded"):
            return self.status
        if float(self.paid_total) > 0 and float(self.balance) <= 0:
            self.status = "paid"
        elif float(self.paid_total) > 0:
            self.status = "partially_paid"
        return self.status


class FeeAdjustment(models.Model):
    student_fee = models.ForeignKey(StudentFee, on_delete=models.CASCADE, related_name="adjustments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)  # +/- delta applied to discount
    reason = models.TextField()
    approved_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
