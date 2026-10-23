from datetime import date

from django.db.models import Count, Sum
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from .engine import compute_payables_for_session, generate_salaries, generate_student_salaries
from .models import (StudentSalaryBand, TrainerFeeAdjustment, TrainerFeeRule, TrainerPayable,
                     TrainerPayment, TrainerSalaryConfig)


def _in_range(qs, frm, to, date_field="session__date"):
    """Payables in range: dated sessions OR overlapping salary periods."""
    from django.db.models import Q
    if not frm and not to:
        return qs
    cond = Q()
    if frm and to:
        cond = Q(**{f"{date_field}__gte": frm, f"{date_field}__lte": to}) | Q(
            period_start__lte=to, period_end__gte=frm)
    elif frm:
        cond = Q(**{f"{date_field}__gte": frm}) | Q(period_end__gte=frm) | Q(
            period_start__isnull=False, period_end__isnull=True)
    else:
        cond = Q(**{f"{date_field}__lte": to}) | Q(period_start__lte=to)
    return qs.filter(cond)


class TrainerFeeRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainerFeeRule
        fields = "__all__"


def _ranges_overlap(a_from, a_to, b_from, b_to):
    a_to = a_to or date.max
    b_to = b_to or date.max
    return a_from <= b_to and b_from <= a_to


class StudentSalaryBandSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentSalaryBand
        fields = ["id", "min_students", "max_students", "amount"]


class TrainerSalaryConfigSerializer(serializers.ModelSerializer):
    """One salary method per trainer per effective period.

    Bands are written nested for student-based banded mode. Overlapping active
    periods for the same trainer are rejected so the method is always
    unambiguous; the change must be a dated new config."""
    bands = StudentSalaryBandSerializer(many=True, required=False)
    trainer_name = serializers.CharField(source="trainer.first_name", read_only=True)
    method_label = serializers.CharField(source="get_method_display", read_only=True)

    class Meta:
        model = TrainerSalaryConfig
        fields = "__all__"
        read_only_fields = ["created_by", "created_at", "updated_at"]

    def validate(self, attrs):
        inst = self.instance
        trainer = attrs.get("trainer", getattr(inst, "trainer", None))
        method = attrs.get("method", getattr(inst, "method", "session_based"))
        frm = attrs.get("effective_from", getattr(inst, "effective_from", None))
        to = attrs.get("effective_to", getattr(inst, "effective_to", None))
        is_active = attrs.get("is_active", getattr(inst, "is_active", True))
        if frm and to and to < frm:
            raise serializers.ValidationError({"effective_to": "Must be on or after effective_from."})
        if trainer and frm and is_active:
            others = TrainerSalaryConfig.objects.filter(trainer=trainer, is_active=True)
            if inst:
                others = others.exclude(pk=inst.pk)
            for o in others:
                if _ranges_overlap(frm, to, o.effective_from, o.effective_to):
                    raise serializers.ValidationError(
                        "This trainer already has an active salary method for an "
                        f"overlapping period ({o.effective_from} to {o.effective_to or 'open'}). "
                        "Close it with an effective_to date before adding a new method.")
        if method == "student_based":
            mode = attrs.get("student_mode", getattr(inst, "student_mode", "per_student"))
            rate = attrs.get("student_rate", getattr(inst, "student_rate", 0))
            if mode == "per_student" and (not rate or float(rate) <= 0):
                raise serializers.ValidationError(
                    {"student_rate": "A per-student rate is required for per-student mode."})
            bands = attrs.get("bands", None)
            if mode == "banded":
                if bands is None and inst:
                    bands = list(inst.bands.values("min_students", "max_students"))
                if not bands:
                    raise serializers.ValidationError(
                        {"bands": "Add at least one student-count band for banded mode."})
                self._validate_bands(bands)
        return attrs

    @staticmethod
    def _validate_bands(bands):
        rows = []
        for b in bands:
            mn = b.get("min_students")
            mx = b.get("max_students")
            if mn is None or mn < 0:
                raise serializers.ValidationError({"bands": "min_students must be zero or more."})
            if mx is not None and mx < mn:
                raise serializers.ValidationError({"bands": "max_students must be >= min_students."})
            rows.append((mn, mx))
        rows.sort()
        for i in range(1, len(rows)):
            prev_max = rows[i - 1][1]
            if prev_max is None or rows[i][0] <= prev_max:
                raise serializers.ValidationError({"bands": "Student-count bands must not overlap."})

    def _save_bands(self, config, bands):
        config.bands.all().delete()
        for b in bands:
            StudentSalaryBand.objects.create(config=config, **b)

    def create(self, validated_data):
        bands = validated_data.pop("bands", [])
        config = TrainerSalaryConfig.objects.create(**validated_data)
        if config.method == "student_based":
            self._save_bands(config, bands)
        return config

    def update(self, instance, validated_data):
        bands = validated_data.pop("bands", None)
        for k, v in validated_data.items():
            setattr(instance, k, v)
        instance.save()
        if instance.method == "student_based" and bands is not None:
            self._save_bands(instance, bands)
        return instance


class TrainerSalaryConfigViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainerSalaryConfig.objects.select_related("trainer", "sport").prefetch_related("bands")
    serializer_class = TrainerSalaryConfigSerializer
    perm_module = "trainer_fees"
    list_perm = "trainer_fees.view"
    retrieve_perm = "trainer_fees.view"
    create_perm = "trainer_fees.manage"
    edit_perm = "trainer_fees.manage"
    destroy_perm = "trainer_fees.manage"
    permission_classes = [HasPerm("trainer_fees.manage")]
    filterset_fields = ["trainer", "method", "is_active"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "trainer__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        ensure_school(self.request, getattr(serializer.validated_data.get("trainer"), "school_id", None),
                      field_name="trainer")
        obj = serializer.save(created_by=self.request.user)
        log_audit(self.request, "trainerfee.config.create", "trainer_fees", f"config:{obj.pk}",
                  new={"trainer": obj.trainer_id, "method": obj.method,
                       "from": str(obj.effective_from), "to": str(obj.effective_to or "")})

    def perform_update(self, serializer):
        obj = serializer.save()
        log_audit(self.request, "trainerfee.config.update", "trainer_fees", f"config:{obj.pk}",
                  new={"trainer": obj.trainer_id, "method": obj.method,
                       "from": str(obj.effective_from), "to": str(obj.effective_to or "")})

    def perform_destroy(self, instance):
        log_audit(self.request, "trainerfee.config.delete", "trainer_fees", f"config:{instance.pk}",
                  old={"trainer": instance.trainer_id, "method": instance.method})
        instance.delete()


class TrainerPayableSerializer(serializers.ModelSerializer):
    session_date = serializers.DateField(source="session.date", read_only=True)
    trainer_name = serializers.CharField(source="trainer.first_name", read_only=True)

    class Meta:
        model = TrainerPayable
        fields = "__all__"
        read_only_fields = ["amount", "verified_by"]


class TrainerPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainerPayment
        fields = "__all__"
        read_only_fields = ["paid_by", "created_at"]


class TrainerFeeAdjustmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainerFeeAdjustment
        fields = "__all__"
        read_only_fields = ["approved_by", "created_at"]


class TrainerFeeRuleViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainerFeeRule.objects.select_related("trainer", "sport", "batch").all()
    serializer_class = TrainerFeeRuleSerializer
    perm_module = "trainer_fees"
    # Rule configuration stays a manage-level action; reads need only view.
    list_perm = "trainer_fees.view"
    retrieve_perm = "trainer_fees.view"
    create_perm = "trainer_fees.manage"
    edit_perm = "trainer_fees.manage"
    destroy_perm = "trainer_fees.manage"
    permission_classes = [HasPerm("trainer_fees.manage")]
    filterset_fields = ["trainer", "sport", "batch", "is_active"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "trainer__school_id")


class TrainerPayableViewSet(viewsets.ModelViewSet):
    queryset = TrainerPayable.objects.select_related("trainer", "session", "rule").all().order_by("-created_at")
    serializer_class = TrainerPayableSerializer
    module = "trainer_fees"
    filterset_fields = ["trainer", "status", "kind"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "summary", "earnings"):
            return [HasPerm("trainer_fees.view")]
        if self.action == "generate":
            return [HasPerm("trainer_fees.generate")]
        if self.action == "verify":
            return [HasPerm("trainer_fees.verify")]
        return [HasPerm("trainer_fees.manage")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        qs = scope_queryset(super().get_queryset(), self.request.user,
                            "session__batch__school_id", "trainer__school_id")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if user.coach_profile_id:
                qs = qs.filter(trainer_id=user.coach_profile_id)
            else:
                qs = qs.none()
        return qs

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("trainer_fees.manage")])
    def generate(self, request):
        """Generate payables for completed sessions (optionally filtered).

        Also generates weekly/monthly salary payables for trainers on fixed
        salary models. Returns generated count plus the sessions skipped for
        lack of a matching active rule — so "sessions taken but ₹0 earned" is
        immediately explainable.
        """
        from datetime import date as d
        from apps.common.scope import user_school_id
        from apps.sessions.models import Session
        qs = Session.objects.filter(status="completed")
        if request.data.get("trainer"):
            qs = qs.filter(coach_id=request.data["trainer"])
        sid = user_school_id(request.user)
        if sid is not None:
            qs = qs.filter(batch__school_id=sid)
        if request.data.get("from_date"):
            qs = qs.filter(date__gte=request.data["from_date"])
        if request.data.get("to_date"):
            qs = qs.filter(date__lte=request.data["to_date"])
        created = 0
        skipped_no_rule = []
        sessions = list(qs.select_related("coach", "batch", "sport")[:500])
        for s in sessions:
            if compute_payables_for_session(s):
                created += 1
            else:
                skipped_no_rule.append(s.pk)
        salaries = 0
        student_salaries = 0
        if request.data.get("from_date") or request.data.get("to_date"):
            trainer_ids = {s.coach_id for s in sessions}
            if request.data.get("trainer"):
                trainer_ids.add(int(request.data["trainer"]))
            frm = request.data.get("from_date") or d(2000, 1, 1).isoformat()
            to = request.data.get("to_date") or d.today().isoformat()
            salaries = generate_salaries(trainer_ids, d.fromisoformat(frm), d.fromisoformat(to))
            student_salaries = generate_student_salaries(trainer_ids, d.fromisoformat(frm), d.fromisoformat(to))
        log_audit(request, "trainerfee.generate", "trainer_fees", "bulk",
                  new={"created": created, "salaries": salaries,
                       "student_salaries": student_salaries,
                       "skipped_no_rule": len(skipped_no_rule)})
        return Response({"generated": created, "salaries": salaries,
                         "student_salaries": student_salaries,
                         "skipped_no_rule": skipped_no_rule})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("trainer_fees.manage")])
    def verify(self, request, pk=None):
        payable = self.get_object()
        payable.status = "verified"
        payable.verified_by = request.user
        payable.save(update_fields=["status", "verified_by"])
        log_audit(request, "trainerfee.verify", "trainer_fees", f"payable:{payable.pk}", new={"status": "verified"})
        return Response(self.get_serializer(payable).data)

    @action(detail=False, methods=["get"])
    def summary(self, request):
        from apps.common.scope import scope_queryset
        from apps.sessions.models import Session
        qs = self.get_queryset()
        trainer = request.query_params.get("trainer")
        if trainer:
            qs = qs.filter(trainer_id=trainer)
        frm = request.query_params.get("from")
        to = request.query_params.get("to")
        qs = _in_range(qs, frm, to)
        agg = qs.values("status").annotate(total=Sum("amount"))
        payable = qs.filter(status__in=["pending", "verified"]).aggregate(t=Sum("amount"))["t"] or 0
        paid = qs.filter(status="paid").aggregate(t=Sum("amount"))["t"] or 0
        by_status = {b["status"]: b["count"] for b in qs.values("status").annotate(count=Count("id"))}
        sessions_qs = Session.objects.filter(status="completed")
        if trainer:
            sessions_qs = sessions_qs.filter(coach_id=trainer)
        if frm:
            sessions_qs = sessions_qs.filter(date__gte=frm)
        if to:
            sessions_qs = sessions_qs.filter(date__lte=to)
        # Obligations split by payment model: session-based payables
        # vs weekly/monthly salary periods. Amounts are live sums,
        # never hardcoded.
        by_kind = {}
        for kind, label in (("session", "Session-based"), ("weekly_salary", "Weekly salary"),
                            ("monthly_salary", "Monthly salary"), ("student_salary", "Student-based salary")):
            kqs = qs.filter(kind=kind)
            obligation = kqs.filter(status__in=["pending", "verified"]).aggregate(t=Sum("amount"))["t"] or 0
            by_kind[kind] = {
                "label": label,
                "count": kqs.count(),
                "obligation": float(obligation),
                "paid": float(kqs.filter(status="paid").aggregate(t=Sum("amount"))["t"] or 0),
                "by_status": {b["status"]: float(b["total"]) for b in kqs.values("status").annotate(total=Sum("amount"))},
            }
        # Trainers per payment model (from active rules) — the
        # academy's configured split, used for the salary dashboard.
        models_qs = scope_queryset(TrainerFeeRule.objects.filter(is_active=True), request.user, "trainer__school_id")
        if trainer:
            models_qs = models_qs.filter(trainer_id=trainer)
        by_model = {}
        for model, label in (("per_session", "Session-based"), ("weekly", "Weekly salary"),
                             ("monthly", "Monthly salary")):
            by_model[model] = {
                "label": label,
                "trainers": models_qs.filter(payment_model=model).values("trainer_id").distinct().count(),
            }
        # Configured salary method per trainer (TrainerSalaryConfig) — the
        # authoritative split once a trainer has a dated salary config.
        cfg_qs = scope_queryset(TrainerSalaryConfig.objects.filter(is_active=True),
                                request.user, "trainer__school_id")
        if trainer:
            cfg_qs = cfg_qs.filter(trainer_id=trainer)
        by_method = {}
        for method, label in (("session_based", "Session-based"), ("student_based", "Student-based"),
                              ("weekly", "Fixed weekly"), ("monthly", "Fixed monthly")):
            by_method[method] = {
                "label": label,
                "trainers": cfg_qs.filter(method=method).values("trainer_id").distinct().count(),
            }
        return Response({
            "by_status": list(agg),
            "by_kind": by_kind,
            "by_payment_model": by_model,
            "by_method": by_method,
            "payable": payable,
            "paid": paid,
            "balance": float(payable),
            "eligible_sessions": qs.exclude(status="cancelled").count(),
            "pending_count": by_status.get("pending", 0),
            "completed_sessions": sessions_qs.count(),
            "trainers": qs.values("trainer_id").distinct().count(),
            "from": frm, "to": to,
        })

    @action(detail=False, methods=["get"])
    def earnings(self, request):
        """Work summary for salary: completed sessions taken + payable/paid
        amounts for a period. Same numbers power the trainer app and admin.

        Params: trainer (pk, optional — coaches are auto-scoped to themselves),
        period=week|month (default month), or explicit from/to (YYYY-MM-DD).
        Week = Mon–Sun, Asia/Kolkata.
        """
        from datetime import date as d, timedelta
        from zoneinfo import ZoneInfo
        from apps.sessions.models import Session
        tz = ZoneInfo("Asia/Kolkata")
        today = d.today()
        try:
            today = __import__("datetime").datetime.now(tz).date()
        except Exception:
            pass
        period = (request.query_params.get("period") or "").lower()
        frm = request.query_params.get("from")
        to = request.query_params.get("to")
        if frm or to:
            from_date = d.fromisoformat(frm) if frm else d(today.year, today.month, 1)
            if to:
                to_date = d.fromisoformat(to)
            elif frm:
                to_date = today
            else:
                to_date = (d(today.year + (today.month == 12), (today.month % 12) + 1, 1) - timedelta(days=1))
        elif period == "week":
            from_date = today - timedelta(days=today.weekday())
            to_date = from_date + timedelta(days=6)
        else:  # month
            period = "month"
            from_date = d(today.year, today.month, 1)
            to_date = (d(today.year + (today.month == 12), (today.month % 12) + 1, 1) - timedelta(days=1))
        trainer = request.query_params.get("trainer")
        user = request.user
        if user.is_authenticated and not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            trainer = user.coach_profile_id  # coaches always see themselves only
            if not trainer:
                return Response({"from": str(from_date), "to": str(to_date), "trainer": None,
                                 "sessions_taken": 0, "payable_amount": 0, "paid_amount": 0, "by_day": []})
        sessions = Session.objects.filter(status="completed", date__gte=from_date, date__lte=to_date)
        payables = _in_range(self.get_queryset(), from_date.isoformat(), to_date.isoformat())
        if trainer:
            sessions = sessions.filter(coach_id=trainer)
            payables = payables.filter(trainer_id=trainer)
        sessions_taken = sessions.count()
        payable_ids = set(payables.values_list("session_id", flat=True))
        sessions_without_payable = sessions.exclude(pk__in=payable_ids).count()
        eligible = payables.exclude(status="cancelled")
        payable_count = eligible.count()
        payable_amount = float(eligible.filter(status__in=["pending", "verified"]).aggregate(t=Sum("amount"))["t"] or 0)
        paid_amount = float(payables.filter(status="paid").aggregate(t=Sum("amount"))["t"] or 0)
        day_counts: dict = {}
        for row in sessions.values("date").annotate(n=Count("id")).order_by("date"):
            day_counts[str(row["date"])] = {"sessions": row["n"], "amount": 0.0}
        for row in payables.values("session__date", "period_start").annotate(t=Sum("amount")):
            key = str(row["session__date"] or row["period_start"] or "?")
            day_counts.setdefault(key, {"sessions": 0, "amount": 0.0})["amount"] = float(row["t"] or 0)
        by_day = [{"date": k, "sessions": v["sessions"], "amount": v["amount"]}
                  for k, v in sorted(day_counts.items())]
        # Salary method + student snapshot so student-based trainers see a
        # meaningful figure (their "sessions taken" is legitimately zero).
        from .engine import active_config
        method = None
        students_counted = 0
        if trainer:
            cfg = active_config(trainer, to_date)
            method = cfg.method if cfg else None
            latest = (payables.filter(kind="student_salary")
                      .order_by("-period_start").values_list("student_count", flat=True).first())
            students_counted = latest or 0
        return Response({"from": str(from_date), "to": str(to_date), "period": period,
                         "trainer": int(trainer) if trainer else None, "method": method,
                         "sessions_taken": sessions_taken, "payable_count": payable_count,
                         "payable_amount": payable_amount,
                         "paid_amount": paid_amount,
                         "balance": float(payable_amount),
                         "students_counted": students_counted,
                         "sessions_without_payable": sessions_without_payable,
                         "by_day": by_day})


class TrainerPaymentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainerPayment.objects.select_related("trainer").all().order_by("-paid_on")
    serializer_class = TrainerPaymentSerializer
    perm_module = "trainer_fees"
    list_perm = "trainer_fees.view"
    retrieve_perm = "trainer_fees.view"
    create_perm = "trainer_fees.pay"
    edit_perm = "trainer_fees.manage"
    destroy_perm = "trainer_fees.manage"
    permission_classes = [HasPerm("trainer_fees.manage")]
    filterset_fields = ["trainer"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "trainer__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        from rest_framework.exceptions import ValidationError
        ensure_school(self.request, getattr(serializer.validated_data.get("trainer"), "school_id", None),
                      field_name="trainer")
        trainer = serializer.validated_data["trainer"]
        outstanding = TrainerPayable.objects.filter(
            trainer=trainer, status="verified").aggregate(t=Sum("amount"))["t"] or 0
        if outstanding <= 0:
            raise ValidationError("Nothing payable: this trainer has no verified unsettled sessions. Verify payables first.")
        obj = serializer.save(paid_by=self.request.user)
        # mark oldest verified payables as paid up to amount
        remaining = float(obj.amount)
        for p in TrainerPayable.objects.filter(trainer=obj.trainer, status="verified").order_by("created_at"):
            if remaining <= 0:
                break
            p.status = "paid"
            p.save(update_fields=["status"])
            remaining -= float(p.amount)
        log_audit(self.request, "trainerfee.pay", "trainer_fees", f"trainerpayment:{obj.pk}",
                  new={"trainer": obj.trainer_id, "amount": str(obj.amount)})


class TrainerFeeAdjustmentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainerFeeAdjustment.objects.all().order_by("-created_at")
    serializer_class = TrainerFeeAdjustmentSerializer
    perm_module = "trainer_fees"
    list_perm = "trainer_fees.view"
    retrieve_perm = "trainer_fees.view"
    create_perm = "trainer_fees.adjust"
    edit_perm = "trainer_fees.manage"
    destroy_perm = "trainer_fees.manage"
    permission_classes = [HasPerm("trainer_fees.manage")]
    filterset_fields = ["trainer"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "trainer__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        reason = serializer.validated_data.get("reason", "")
        if not reason:
            raise serializers.ValidationError("Adjustment requires a reason.")
        ensure_school(self.request, getattr(serializer.validated_data.get("trainer"), "school_id", None),
                      field_name="trainer")
        obj = serializer.save(approved_by=self.request.user)
        log_audit(self.request, "trainerfee.adjust", "trainer_fees", f"adjust:{obj.pk}",
                  new={"trainer": obj.trainer_id, "amount": str(obj.amount), "reason": reason})
