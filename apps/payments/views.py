import uuid
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import HasPerm
from .gateway import get_gateway
from .models import Payment, Refund


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = "__all__"
        read_only_fields = ["status", "verified", "verified_at", "receipt_no", "provider",
                            "provider_order_id", "created_at"]


class RefundSerializer(serializers.ModelSerializer):
    class Meta:
        model = Refund
        fields = "__all__"
        read_only_fields = ["approved_by", "created_at"]


class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.select_related("student_fee").all().order_by("-created_at")
    serializer_class = PaymentSerializer
    module = "payments"
    filterset_fields = ["student_fee", "status", "verified", "provider"]
    search_fields = ["provider_payment_id", "provider_order_id", "receipt_no"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = scope_queryset(super().get_queryset(), self.request.user,
                            "student_fee__student__school_id")
        # Parent-role accounts see only payment receipts for their own linked students.
        return scope_to_parent(qs, self.request.user, "student_fee__student_id")

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [HasPerm("payments.view")]
        if self.action == "create":
            return [HasPerm("payments.create")]
        if self.action == "refund":
            return [HasPerm("payments.refund")]
        return [HasPerm("payments.verify")]

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        gateway = get_gateway()
        fee = serializer.validated_data["student_fee"]
        ensure_school(self.request, getattr(fee.student, "school_id", None), field_name="student_fee")
        amount = serializer.validated_data.get("amount") or fee.balance
        key = f"pay_{uuid.uuid4().hex}"
        with transaction.atomic():
            payment = serializer.save(provider=gateway.name, amount=amount, idempotency_key=key, status="pending")
            order = gateway.create_order(amount=str(amount), currency=payment.currency,
                                         receipt=f"fee_{fee.pk}", idempotency_key=key)
            payment.provider_order_id = order["order_id"]
            payment.save(update_fields=["provider_order_id"])
            log_audit(self.request, "payment.initiate", "payments", f"payment:{payment.pk}",
                      new={"amount": str(amount), "provider": gateway.name})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("payments.verify")])
    def refund(self, request, pk=None):
        from decimal import Decimal, InvalidOperation
        payment = self.get_object()
        if payment.status != "success":
            return Response({"detail": "Only successful payments can be refunded."}, status=400)
        try:
            amount = Decimal(str(request.data.get("amount", payment.amount)))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"detail": "Invalid refund amount."}, status=400)
        if amount <= 0 or amount > payment.amount:
            return Response({"detail": "Refund amount must be greater than 0 and cannot exceed the payment amount."}, status=400)
        reason = request.data.get("reason", "")
        if not reason:
            return Response({"detail": "Refund reason required."}, status=400)
        with transaction.atomic():
            r = Refund.objects.create(payment=payment, amount=amount, reason=reason,
                                      approved_by=request.user, status="approved")
            # A full refund voids the payment (so it no longer counts as
            # collected); a partial refund leaves it successful.
            if amount >= payment.amount:
                payment.status = "refunded"
                payment.save(update_fields=["status"])
            fee = payment.student_fee
            fee.refresh_status()
            if float(fee.paid_total) <= 0 and fee.status in ("paid", "partially_paid"):
                fee.status = "due"
            fee.save(update_fields=["status"])
            log_audit(request, "payment.refund", "payments", f"payment:{payment.pk}",
                      old={"status": "success"},
                      new={"status": payment.status, "reason": reason, "amount": str(amount)})
        return Response(RefundSerializer(r).data, status=201)


@api_view(["POST"])
@permission_classes([AllowAny])  # verified by gateway signature, not session auth
def webhook(request):
    """Trusted gateway callback. Verifies signature server-side before marking success."""
    gateway = get_gateway()
    payload = request.data if isinstance(request.data, dict) else {}
    signature = ""
    for header in getattr(gateway, "signature_headers", ("X-Signature",)):
        signature = request.headers.get(header) or signature
    signature = signature or payload.get("signature", "")
    raw_body = request.body or b""
    order_id = payload.get("order_id", "")
    payment_id = payload.get("payment_id", "")
    if not order_id:
        return Response({"detail": "order_id required."}, status=400)
    payment = Payment.objects.select_related("student_fee").filter(provider_order_id=order_id).first()
    if payment is None:
        return Response({"detail": "Unknown order."}, status=404)
    # Idempotency: already-verified payments are never overwritten
    if payment.verified and payment.status == "success":
        return Response({"detail": "Already processed.", "payment": payment.pk})
    if not gateway.verify_webhook(payload=payload, signature=signature, raw_body=raw_body):
        payment.status = "failed"
        payment.raw_callback = payload
        payment.save(update_fields=["status", "raw_callback"])
        return Response({"detail": "Signature verification failed."}, status=400)
    with transaction.atomic():
        payment.provider_payment_id = payment_id
        payment.provider_signature = signature
        payment.status = "success"
        payment.verified = True
        payment.verified_at = timezone.now()
        payment.receipt_no = payment.receipt_no or f"RCP-{timezone.now():%Y%m%d}-{payment.pk:06d}"
        payment.raw_callback = payload
        payment.save()
        fee = payment.student_fee
        fee.refresh_status()
        fee.save(update_fields=["status"])
    log_audit(request, "payment.verify", "payments", f"payment:{payment.pk}", new={"status": "success"})
    return Response({"detail": "Payment verified.", "payment": payment.pk, "receipt": payment.receipt_no})
