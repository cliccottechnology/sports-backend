from datetime import date
from decimal import Decimal

import uuid
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from .models import FeeAdjustment, FeePlan, StudentFee


class FeePlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeePlan
        fields = "__all__"


class StudentFeeSerializer(serializers.ModelSerializer):
    paid_total = serializers.ReadOnlyField()
    balance = serializers.ReadOnlyField()
    student_name = serializers.CharField(source="student.first_name", read_only=True)

    class Meta:
        model = StudentFee
        fields = "__all__"


class FeeAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = FeeAdjustment
        fields = "__all__"
        read_only_fields = ["approved_by", "created_at"]


class FeePlanViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = FeePlan.objects.all().order_by("name")
    serializer_class = FeePlanSerializer
    perm_module = "fees"
    filterset_fields = ["cycle", "sport", "is_active"]


class StudentFeeViewSet(viewsets.ModelViewSet):
    queryset = StudentFee.objects.select_related("student", "fee_plan", "batch").all().order_by("-due_date")
    serializer_class = StudentFeeSerializer
    module = "fees"
    filterset_fields = ["student", "fee_plan", "batch", "status", "period_label"]
    search_fields = ["student__first_name", "student__student_id", "period_label"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = scope_queryset(super().get_queryset(), self.request.user, "student__school_id")
        return scope_to_parent(qs, self.request.user, "student_id")

    def get_permissions(self):
        # list/retrieve/summary/overdue → fees.view; create → fees.create;
        # update → fees.edit; destroy → fees.manage (money records);
        # adjust → fees.adjust; record_payment → payments.verify.
        if self.action in ("list", "retrieve", "summary", "overdue"):
            return [HasPerm("fees.view")]
        if self.action == "create":
            return [HasPerm("fees.create")]
        if self.action in ("update", "partial_update", "mark_overdue"):
            return [HasPerm("fees.edit")]
        if self.action == "record_payment":
            return [HasPerm("payments.verify")]
        if self.action == "adjust":
            return [HasPerm("fees.adjust")]
        return [HasPerm("fees.manage")]

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        student = serializer.validated_data.get("student")
        ensure_school(self.request, getattr(student, "school_id", None), field_name="student")
        obj = serializer.save(created_by=self.request.user)
        log_audit(self.request, "fee.allocate", "fees", f"studentfee:{obj.pk}",
                  new={"student": obj.student_id, "amount": str(obj.amount)})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("fees.manage")])
    def adjust(self, request, pk=None):
        fee = self.get_object()
        amount = request.data.get("amount")
        reason = request.data.get("reason", "")
        if amount is None or not reason:
            return Response({"detail": "amount and reason required."}, status=400)
        adj = FeeAdjustment.objects.create(student_fee=fee, amount=amount, reason=reason, approved_by=request.user)
        fee.discount = float(fee.discount) + float(amount)
        fee.save(update_fields=["discount"])
        log_audit(request, "fee.adjust", "fees", f"studentfee:{fee.pk}",
                  old={"discount": str(float(fee.discount) - float(amount))},
                  new={"discount": str(fee.discount), "reason": reason})
        return Response(FeeAdjustmentSerializer(adj).data, status=201)

    @action(detail=False, methods=["get"])
    def overdue(self, request):
        qs = self.get_queryset().filter(due_date__lt=date.today()).exclude(status__in=["paid", "cancelled", "refunded"])
        page = self.paginate_queryset(qs)
        ser = self.get_serializer(page or qs, many=True)
        return self.get_paginated_response(ser.data) if page is not None else Response(ser.data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts + amounts by status, plus total collected and outstanding."""
        from django.db.models import F
        from apps.payments.models import Payment
        qs = self.get_queryset()
        by_status = list(qs.values("status").annotate(count=Count("id"), amount=Sum("amount")).order_by("status"))
        collected = Payment.objects.filter(student_fee__in=qs, status="success").aggregate(t=Sum("amount"))["t"] or 0
        # Billed is net of discounts; otherwise a discounted fee reports its full
        # face value as outstanding.
        billed = qs.aggregate(t=Sum(F("amount") - F("discount")))["t"] or 0
        return Response({"by_status": by_status, "collected": float(collected),
                         "billed": float(billed), "outstanding": float(billed) - float(collected)})

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("fees.edit")])
    def mark_overdue(self, request):
        """Flip all past-due unpaid fees to overdue. Returns how many changed."""
        n = (self.get_queryset().filter(due_date__lt=date.today())
             .exclude(status__in=["paid", "cancelled", "refunded", "overdue"])
             .update(status="overdue"))
        log_audit(request, "fee.mark_overdue", "fees", "bulk", new={"marked": n})
        return Response({"marked_overdue": n})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("payments.verify")])
    def record_payment(self, request, pk=None):
        """Record an offline payment (cash / UPI / bank transfer received directly).

        Creates a verified successful Payment, refreshes the fee status to
        paid or partially_paid, and audit-logs everything. Online gateway
        payments still go through the webhook only — this is strictly for
        money received outside the gateway.
        """
        fee = self.get_object()
        if fee.status in ("cancelled", "refunded"):
            return Response({"detail": f"Cannot take payment on a {fee.status} fee."}, status=400)
        raw = request.data.get("amount", None)
        try:
            amount = float(fee.balance) if raw in (None, "") else float(raw)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid amount."}, status=400)
        if amount <= 0:
            return Response({"detail": "Amount must be positive."}, status=400)
        if amount - float(fee.balance) > 0.01:
            return Response({"detail": f"Amount exceeds the outstanding balance of {fee.balance}."}, status=400)
        method = (request.data.get("method") or "cash").strip().lower()
        if method not in ("cash", "upi", "bank_transfer"):
            return Response({"detail": "Method must be cash, upi or bank_transfer."}, status=400)
        reference = str(request.data.get("reference") or "").strip()
        with transaction.atomic():
            from apps.payments.models import Payment
            pay = Payment.objects.create(
                student_fee=fee, amount=Decimal(str(amount)), currency="INR", provider="offline",
                status="success", verified=True, verified_at=timezone.now(),
                idempotency_key=f"off_{uuid.uuid4().hex}",
                raw_callback={"method": method, "reference": reference,
                              "recorded_by": getattr(request.user, "username", "")})
            pay.receipt_no = f"OFF-{timezone.now():%Y%m%d}-{pay.pk:06d}"
            pay.save(update_fields=["receipt_no"])
            fee.refresh_status()
            fee.save(update_fields=["status"])
        log_audit(request, "fee.offline_payment", "fees", f"studentfee:{fee.pk}",
                  new={"amount": str(amount), "method": method, "receipt": pay.receipt_no,
                       "status": fee.status})
        return Response({"payment": pay.pk, "receipt": pay.receipt_no,
                         "fee_status": fee.status, "balance": fee.balance}, status=201)
