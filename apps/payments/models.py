"""Payments. Fee is marked paid ONLY after backend webhook verification — never on frontend callback."""
from django.db import models

PAY_STATUS = [("initiated", "Initiated"), ("pending", "Pending"), ("success", "Success"),
              ("failed", "Failed"), ("refunded", "Refunded")]


class Payment(models.Model):
    student_fee = models.ForeignKey("fees.StudentFee", on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="INR")
    provider = models.CharField(max_length=30, default="mock")  # mock|razorpay|stripe
    provider_order_id = models.CharField(max_length=200, blank=True)
    provider_payment_id = models.CharField(max_length=200, blank=True, db_index=True)
    provider_signature = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=20, choices=PAY_STATUS, default="initiated", db_index=True)
    verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    receipt_no = models.CharField(max_length=50, unique=True, null=True, blank=True)
    idempotency_key = models.CharField(max_length=100, unique=True)
    raw_callback = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Payment {self.pk} {self.amount} {self.status}"


class Refund(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField()
    status = models.CharField(max_length=20, default="pending")
    approved_by = models.ForeignKey("accounts.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
