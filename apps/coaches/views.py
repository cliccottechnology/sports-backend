from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from .models import (Coach, CoachAssignment, CoachAvailability, CoachSport,
                     TrainerAttendanceCorrection, TrainerCertificate, TrainerCheckIn, TrainerLeave)


class TrainerCheckInSerializer(serializers.ModelSerializer):
    session_date = serializers.DateField(source="session.date", read_only=True)
    batch_name = serializers.CharField(source="session.batch.name", read_only=True)
    coach_name = serializers.CharField(source="coach.first_name", read_only=True)
    trainer_id = serializers.CharField(source="coach.trainer_id", read_only=True)
    correction_status = serializers.SerializerMethodField()
    correction_id = serializers.SerializerMethodField()
    deadline = serializers.SerializerMethodField()
    can_request_correction = serializers.SerializerMethodField()

    class Meta:
        model = TrainerCheckIn
        fields = "__all__"
        read_only_fields = ["coach", "marked_by", "marked_at", "auto_marked"]

    def _latest_correction(self, obj):
        return obj.corrections.order_by("-requested_at").first()

    def get_correction_status(self, obj):
        c = self._latest_correction(obj)
        return c.status if c else None

    def get_correction_id(self, obj):
        c = self._latest_correction(obj)
        return c.pk if c else None

    def get_deadline(self, obj):
        from .attendance_automation import attendance_deadline, config_for
        school_id = getattr(getattr(obj.session, "batch", None), "school_id", None)
        cfg = config_for(school_id)
        try:
            return attendance_deadline(obj.session, cfg.get("trainer.attendance_grace_minutes", 15)).isoformat()
        except Exception:
            return None

    def get_can_request_correction(self, obj):
        from .attendance_automation import config_for
        if not obj.auto_marked and obj.status != "absent":
            return False
        latest = self._latest_correction(obj)
        if latest and latest.status == "pending":
            return False
        school_id = getattr(getattr(obj.session, "batch", None), "school_id", None)
        cfg = config_for(school_id)
        days = cfg.get("trainer.correction_deadline_days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        if not obj.session_id:
            return False
        from datetime import timedelta
        from django.utils import timezone
        return timezone.localdate() <= obj.session.date + timedelta(days=days)


class TrainerCheckInViewSet(viewsets.ModelViewSet):
    """Trainers check in to their OWN assigned sessions only.

    Coaches may list/create/update their OWN check-ins (so a mistaken mark can
    be corrected). Deleting and other coaches' records require coaches.manage.
    Academy admins read all of their school's check-ins for oversight.
    """
    serializer_class = TrainerCheckInSerializer
    module = "coaches"
    filterset_fields = ["coach", "session", "status", "auto_marked"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "create", "update", "partial_update"):
            return [IsAuthenticated()]
        return [HasPerm("coaches.manage")]

    def get_queryset(self):
        qs = TrainerCheckIn.objects.select_related(
            "session__batch", "coach").all().order_by("-marked_at")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser:
            role = getattr(getattr(user, "role", None), "name", None)
            if role == "coach":
                qs = qs.filter(coach_id=user.coach_profile_id) if user.coach_profile_id else qs.none()
            elif not (user.has_perm_code("coaches.view") or user.has_perm_code("coaches.manage")):
                qs = qs.none()
        # Date-range window (admin oversight page) — plain query params so the
        # filter is honoured alongside the exact-match filterset fields above.
        date_from = self.request.query_params.get("date_from")
        date_to = self.request.query_params.get("date_to")
        if date_from:
            qs = qs.filter(session__date__gte=date_from)
        if date_to:
            qs = qs.filter(session__date__lte=date_to)
        from apps.common.scope import scope_queryset
        return scope_queryset(qs, user, "session__batch__school_id")

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied, ValidationError
        user = self.request.user
        session = serializer.validated_data["session"]
        privileged = user.is_superuser or user.has_perm_code("coaches.manage")
        if privileged:
            raw_coach = self.request.data.get("coach") or getattr(user, "coach_profile_id", None)
            if raw_coach is None:
                raise ValidationError("coach is required.")
            coach_id = raw_coach.pk if hasattr(raw_coach, "pk") else raw_coach
        else:
            coach_id = getattr(user, "coach_profile_id", None)
            if not coach_id:
                raise PermissionDenied("No coach profile linked to this account.")
            if session.coach_id != coach_id:
                raise PermissionDenied("You can only check in to your own assigned sessions.")
        try:
            obj = serializer.save(coach_id=coach_id, marked_by=user)
        except Exception as e:
            from django.db import IntegrityError
            if isinstance(e, IntegrityError):
                raise ValidationError("Already checked in to this session.")
            raise
        log_audit(self.request, "trainer.checkin", "coaches", f"session:{session.pk}",
                  new={"coach": coach_id, "status": obj.status})

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        user = self.request.user
        obj = self.get_object()
        privileged = user.is_superuser or user.has_perm_code("coaches.manage")
        if not privileged:
            if not getattr(user, "coach_profile_id", None) or obj.coach_id != user.coach_profile_id:
                raise PermissionDenied("You can only correct your own check-ins.")
            if obj.auto_marked:
                raise PermissionDenied(
                    "This absence was recorded automatically. Submit an attendance correction request instead.")
        old_status = obj.status
        # Identity (coach/session) is immutable once recorded; only the status
        # and notes may be corrected.
        obj = serializer.save(coach=obj.coach, session=obj.session)
        if old_status != obj.status:
            log_audit(self.request, "trainer.checkin_correct", "coaches", f"session:{obj.session_id}",
                      old={"status": old_status}, new={"status": obj.status})


class TrainerLeaveSerializer(serializers.ModelSerializer):
    coach_name = serializers.CharField(source="coach.first_name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = TrainerLeave
        fields = "__all__"
        read_only_fields = ["requested_by", "reviewed_by", "reviewed_at", "created_at"]


class TrainerLeaveViewSet(GranularPermissions, viewsets.ModelViewSet):
    """Trainer leave requests. A coach creates/lists their own; admins approve."""
    queryset = TrainerLeave.objects.select_related("coach", "session").all()
    serializer_class = TrainerLeaveSerializer
    perm_module = "coaches"
    list_perm = "coaches.view"
    retrieve_perm = "coaches.view"
    create_perm = "coaches.view"      # any signed-in coach may request
    edit_perm = "coaches.manage"
    destroy_perm = "coaches.manage"
    filter_scope = ("coach__school_id",)

    def get_permissions(self):
        if self.action in ("list", "retrieve", "create"):
            return [IsAuthenticated()]
        return [HasPerm("coaches.manage")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        qs = scope_queryset(super().get_queryset(), self.request.user, "coach__school_id")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser:
            if getattr(getattr(user, "role", None), "name", None) == "coach":
                return qs.filter(coach_id=user.coach_profile_id) if user.coach_profile_id else qs.none()
            if not (user.has_perm_code("coaches.view") or user.has_perm_code("coaches.manage")):
                return qs.none()
        return qs

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        user = self.request.user
        privileged = user.is_superuser or user.has_perm_code("coaches.manage")
        raw = serializer.validated_data.get("coach")
        if not privileged:
            coach_id = getattr(user, "coach_profile_id", None)
            if not coach_id or (raw and raw.pk != coach_id):
                raise PermissionDenied("You can only request leave for yourself.")
        obj = serializer.save(requested_by=user)
        log_audit(self.request, "trainer.leave_request", "coaches", f"leave:{obj.pk}",
                  new={"coach": obj.coach_id, "date": str(obj.date)})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("coaches.manage")])
    def decide(self, request, pk=None):
        """Approve/reject a leave request. Body: {status: approved|rejected, note?}."""
        from django.utils import timezone
        decision = (request.data.get("status") or "").lower()
        if decision not in ("approved", "rejected"):
            return Response({"detail": "status must be 'approved' or 'rejected'."}, status=400)
        leave = self.get_object()
        leave.status = decision
        leave.reviewed_by = request.user
        leave.reviewed_at = timezone.now()
        leave.review_note = request.data.get("note", "")
        leave.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
        log_audit(request, "trainer.leave_decide", "coaches", f"leave:{leave.pk}",
                  new={"status": decision})
        return Response(self.get_serializer(leave).data)


class TrainerAttendanceCorrectionSerializer(serializers.ModelSerializer):
    coach_name = serializers.CharField(source="coach.first_name", read_only=True)
    session_date = serializers.DateField(source="session.date", read_only=True)
    batch_name = serializers.CharField(source="session.batch.name", read_only=True)

    class Meta:
        model = TrainerAttendanceCorrection
        fields = "__all__"
        read_only_fields = ["coach", "session", "original_status", "status",
                            "reviewed_by", "reviewed_at", "created_at"]


class TrainerAttendanceCorrectionViewSet(GranularPermissions, viewsets.ModelViewSet):
    """Trainer attendance correction requests (fixing an auto-marked absence)."""
    queryset = TrainerAttendanceCorrection.objects.select_related(
        "coach", "session__batch").all()
    serializer_class = TrainerAttendanceCorrectionSerializer
    module = "coaches"
    filterset_fields = ["coach", "status", "checkin"]

    def get_permissions(self):
        if self.action in ("list", "retrieve", "create"):
            return [IsAuthenticated()]
        if self.action in ("approve", "reject"):
            return [HasPerm("attendance.correct")]
        return [HasPerm("coaches.manage")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        qs = scope_queryset(super().get_queryset(), self.request.user, "coach__school_id")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser:
            if getattr(getattr(user, "role", None), "name", None) == "coach":
                return qs.filter(coach_id=user.coach_profile_id) if user.coach_profile_id else qs.none()
            if not (user.has_perm_code("coaches.view") or user.has_perm_code("coaches.manage")
                    or user.has_perm_code("attendance.correct")):
                return qs.none()
        return qs

    def _approval_allowed(self, user, school_id):
        if user.is_superuser:
            return True
        if user.has_perm_code("attendance.correct"):
            return True
        from .attendance_automation import config_for
        roles = config_for(school_id).get("trainer.approval_roles") or []
        return getattr(getattr(user, "role", None), "name", None) in roles

    def perform_create(self, serializer):
        from datetime import timedelta
        from django.utils import timezone
        from rest_framework.exceptions import PermissionDenied, ValidationError
        user = self.request.user
        checkin = serializer.validated_data.get("checkin")
        if checkin is None:
            raise ValidationError({"checkin": "A check-in is required."})
        privileged = user.is_superuser or user.has_perm_code("coaches.manage")
        if not privileged:
            if not getattr(user, "coach_profile_id", None) or checkin.coach_id != user.coach_profile_id:
                raise PermissionDenied("You can only request a correction for your own attendance.")
        if not (checkin.auto_marked or checkin.status == "absent"):
            raise ValidationError("This check-in is not an automatically marked absence.")
        if checkin.corrections.filter(status="pending").exists():
            raise ValidationError("A correction request for this session is already pending.")
        school_id = getattr(getattr(checkin.session, "batch", None), "school_id", None)
        from .attendance_automation import config_for
        days = config_for(school_id).get("trainer.correction_deadline_days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        if timezone.localdate() > checkin.session.date + timedelta(days=days):
            raise ValidationError("The correction request window for this session has closed.")
        obj = serializer.save(coach=checkin.coach, session=checkin.session,
                              original_status=checkin.status)
        log_audit(self.request, "trainer.correction_request", "coaches", f"correction:{obj.pk}",
                  new={"checkin": checkin.pk, "requested_status": obj.requested_status})

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        correction = self.get_object()
        school_id = getattr(correction.coach, "school_id", None)
        if not self._approval_allowed(request.user, school_id):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You are not permitted to approve attendance corrections.")
        if correction.status != "pending":
            return Response({"detail": "This request has already been decided."}, status=400)
        from django.utils import timezone
        checkin = correction.checkin
        old_status = checkin.status
        if request.data.get("requested_status"):
            override = request.data["requested_status"]
            if override not in ("present", "late", "leave"):
                return Response({"detail": "requested_status must be present, late or leave."}, status=400)
            correction.requested_status = override
        checkin.status = correction.requested_status
        checkin.auto_marked = False
        checkin.notes = (checkin.notes + " | " if checkin.notes else "") + "Corrected via approved request"
        checkin.save(update_fields=["status", "auto_marked", "notes"])
        correction.status = "approved"
        correction.reviewed_by = request.user
        correction.reviewed_at = timezone.now()
        correction.review_note = request.data.get("note", "")
        correction.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note",
                                       "requested_status"])
        log_audit(request, "trainer.correction_approve", "coaches", f"correction:{correction.pk}",
                  old={"status": old_status}, new={"status": checkin.status})
        return Response(self.get_serializer(correction).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        correction = self.get_object()
        school_id = getattr(correction.coach, "school_id", None)
        if not self._approval_allowed(request.user, school_id):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You are not permitted to reject attendance corrections.")
        if correction.status != "pending":
            return Response({"detail": "This request has already been decided."}, status=400)
        note = (request.data.get("note") or "").strip()
        if not note:
            return Response({"detail": "A reason is required to reject a correction request."}, status=400)
        from django.utils import timezone
        correction.status = "rejected"
        correction.reviewed_by = request.user
        correction.reviewed_at = timezone.now()
        correction.review_note = note
        correction.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note"])
        log_audit(request, "trainer.correction_reject", "coaches", f"correction:{correction.pk}",
                  new={"reason": note})
        return Response(self.get_serializer(correction).data)


class TrainerAttendanceConfigView(APIView):
    """Super Admin configuration for automatic trainer-absence handling.

    Reads/writes the AcademySetting keys (platform-level by default, or one
    academy via ?school=). Gated by settings.view / settings.edit and audited.
    """

    def get_permissions(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [HasPerm("settings.view")]
        return [HasPerm("settings.edit")]

    def _school(self, request):
        from apps.common.scope import user_school_id
        sid = user_school_id(request.user)
        if sid is not None:
            return sid
        raw = request.query_params.get("school") or request.data.get("school")
        try:
            return int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def get(self, request):
        from apps.academy.models import get_setting
        from .attendance_automation import CONFIG_META, DEFAULT_SETTINGS
        school_id = self._school(request)
        rows = []
        for key, label, kind, description in CONFIG_META:
            default = DEFAULT_SETTINGS[key]
            value = get_setting(key, school_id, default)
            rows.append({"key": key, "label": label, "type": kind, "description": description,
                         "value": default if value is None else value, "default": default})
        return Response({"school": school_id, "settings": rows})

    def put(self, request):
        from apps.academy.models import AcademySetting
        from .attendance_automation import DEFAULT_SETTINGS
        school_id = self._school(request)
        payload = request.data.get("settings") or request.data
        if not isinstance(payload, dict):
            return Response({"detail": "Provide a settings mapping."}, status=400)
        changed = {}
        for key, value in payload.items():
            if key not in DEFAULT_SETTINGS or key == "school":
                continue
            changed[key] = value
            AcademySetting.objects.update_or_create(
                key=key, school_id=school_id,
                defaults={"value": value, "description": "Trainer attendance automation"})
        if changed:
            log_audit(request, "trainer.attendance_config", "settings", f"school:{school_id or 'platform'}",
                      new=changed)
        return self.get(request)


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def validate_off_days(days):
    """Manual week-off rule: exactly two distinct weekdays (0=Mon..6=Sun)."""
    if days is None:
        return None
    if not isinstance(days, list) or len(days) != 2 or len(set(days)) != 2:
        raise serializers.ValidationError("Choose exactly two weekly off-days.")
    if any(not isinstance(d, int) or d < 0 or d > 6 for d in days):
        raise serializers.ValidationError("Off-days must be weekdays 0 (Mon) to 6 (Sun).")
    return sorted(days)


class CoachSerializer(serializers.ModelSerializer):
    sports = serializers.SerializerMethodField()
    school_name = serializers.CharField(source="school.name", read_only=True)

    class Meta:
        model = Coach
        fields = "__all__"

    def get_sports(self, obj):
        return list(obj.sport_links.values_list("sport__name", flat=True))

    def validate_weekly_off_days(self, value):
        # Empty = unset (preserves older records); any manual selection must be exactly two days.
        if value in (None, []):
            return value
        return validate_off_days(value)


class CoachSportSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoachSport
        fields = "__all__"


class CoachAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = CoachAvailability
        fields = "__all__"


class CoachAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = CoachAssignment
        fields = "__all__"

    def validate(self, attrs):
        from datetime import date
        coach = attrs.get("coach") or getattr(self.instance, "coach", None)
        assigned_from = attrs.get("assigned_from") or getattr(self.instance, "assigned_from", None)
        if coach and assigned_from and isinstance(assigned_from, date):
            if assigned_from.weekday() in (coach.weekly_off_days or []):
                raise serializers.ValidationError("Coach should not normally be assigned on a weekly off-day.")
        return attrs


class CoachViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Coach.objects.select_related("school").all().order_by("first_name")
    serializer_class = CoachSerializer
    perm_module = "coaches"
    filterset_fields = ["is_active", "school"]
    search_fields = ["trainer_id", "first_name", "last_name", "phone", "email"]

    def get_permissions(self):
        # NOTE: action-level `permission_classes` on @action are NOT honored here —
        # this override decides everything, so self-service actions stay public to
        # any signed-in coach while CRUD follows the granular matrix.
        if self.action in ("me", "set_my_off_days", "update_my_profile"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        given = serializer.validated_data.pop("school", None)
        coach = serializer.save(school_id=ensure_school(self.request, given))
        log_audit(self.request, "coach.create", "coaches", f"coach:{coach.pk}",
                  new={"trainer_id": coach.trainer_id})

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "school" in serializer.validated_data:
            ensure_school(self.request, serializer.validated_data.get("school"))
        old = {f: getattr(serializer.instance, f) for f in ("first_name", "last_name", "phone", "email")}
        coach = serializer.save()
        changed = {f: getattr(coach, f) for f in old if getattr(coach, f) != old[f]}
        if changed:
            # Keep the linked login account in sync so app headers update too.
            from apps.accounts.models import User
            User.objects.filter(coach_profile=coach).update(**{f: v for f, v in changed.items() if f in ("first_name", "last_name", "phone", "email")})
            log_audit(self.request, "coach.update", "coaches", f"coach:{coach.pk}",
                      old={k: old[k] for k in changed}, new=changed)

    @action(detail=False, methods=["get"])
    def me(self, request):
        """Return the logged-in coach's own record (sports, off-days)."""
        coach_id = getattr(request.user, "coach_profile_id", None)
        if not coach_id and request.user.is_authenticated:
            coach = (
                Coach.objects.filter(login_account=request.user).first()
                or Coach.objects.filter(email__iexact=request.user.email, email__gt="").first()
                or Coach.objects.filter(phone=request.user.phone, phone__gt="").first()
                or Coach.objects.filter(trainer_id__iexact=request.user.username).first()
            )
            if not coach and Coach.objects.count() == 1:
                coach = Coach.objects.first()
            if coach:
                request.user.coach_profile = coach
                request.user.save(update_fields=["coach_profile"])
                coach_id = coach.id
        if not coach_id:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        try:
            coach = Coach.objects.get(pk=coach_id)
        except Coach.DoesNotExist:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        return Response(self.get_serializer(coach).data)

    @action(detail=False, methods=["patch"])
    def update_my_profile(self, request):
        """Self-service edit: coaches may update their own contact details only.

        Accepted: first_name, last_name, phone, email, photo (image upload).
        Everything else (trainer_id, sports, assignments, status) is ignored —
        admin-managed. The linked login account is synced so headers stay
        consistent.
        """
        coach_id = getattr(request.user, "coach_profile_id", None)
        if not coach_id:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        try:
            coach = Coach.objects.get(pk=coach_id)
        except Coach.DoesNotExist:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        changed = {}
        photo = request.data.get("photo")
        if photo is not None and hasattr(photo, "read"):
            from django.conf import settings as dj_settings
            ext = (getattr(photo, "name", "").rsplit(".", 1)[-1] if "." in getattr(photo, "name", "") else "").lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                return Response({"detail": "Photo must be JPG, PNG, or WEBP."}, status=400)
            if getattr(photo, "size", 0) > int(getattr(dj_settings, "MAX_UPLOAD_SIZE_MB", 10)) * 1024 * 1024:
                return Response({"detail": "Photo too large (max 10 MB)."}, status=400)
            coach.photo.save(getattr(photo, "name", "coach.jpg"), photo, save=False)
            changed["photo"] = coach.photo.name
        for field in ("first_name", "last_name", "phone", "email"):
            if field in request.data:
                value = request.data.get(field) or ""
                value = str(value).strip()
                if field == "email" and value:
                    from django.core.exceptions import ValidationError as VE
                    from django.core.validators import validate_email
                    try:
                        validate_email(value)
                    except VE:
                        return Response({"detail": "Enter a valid email address."}, status=400)
                if getattr(coach, field) != value:
                    setattr(coach, field, value)
                    changed[field] = value
        if changed:
            coach.save(update_fields=list(changed))
            user = request.user
            for field in ("first_name", "last_name", "phone", "email"):
                if field in changed and hasattr(user, field):
                    setattr(user, field, changed[field])
            user.save(update_fields=[f for f in changed if hasattr(user, f)] or ["username"])
            log_audit(request, "coach.profile_update", "coaches", f"coach:{coach.pk}", new=changed)
        return Response({
            "coach": self.get_serializer(coach).data,
            "user": {"first_name": request.user.first_name, "last_name": request.user.last_name,
                     "email": request.user.email, "phone": getattr(request.user, "phone", "")},
            "changed": changed,
        })

    @action(detail=False, methods=["post"], permission_classes=[IsAuthenticated])
    def set_my_off_days(self, request):
        """Let a trainer manually pick their own two weekly off-days (0=Mon..6=Sun)."""
        coach_id = getattr(request.user, "coach_profile_id", None)
        if not coach_id:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        try:
            coach = Coach.objects.get(pk=coach_id)
        except Coach.DoesNotExist:
            return Response({"detail": "No coach profile linked to this account."}, status=404)
        try:
            days = validate_off_days(request.data.get("weekly_off_days"))
        except serializers.ValidationError as e:
            return Response({"detail": e.detail[0] if isinstance(e.detail, list) else e.detail}, status=400)
        if days is None:
            return Response({"detail": "weekly_off_days is required: pick exactly two days."}, status=400)
        old = list(coach.weekly_off_days or [])
        coach.weekly_off_days = days
        coach.save(update_fields=["weekly_off_days"])
        log_audit(request, "coach.offdays_set", "coaches", f"coach:{coach.pk}",
                  old={"weekly_off_days": old}, new={"weekly_off_days": days})
        return Response(self.get_serializer(coach).data)


class TrainerCertificateSerializer(serializers.ModelSerializer):
    trainer_name = serializers.CharField(source="trainer.first_name", read_only=True)

    class Meta:
        model = TrainerCertificate
        fields = "__all__"
        read_only_fields = ["sessions_completed", "issued_by", "issued_at"]


def _render_certificate_pdf(cert):
    """Institute certificate PDF. Returns bytes (reportlab)."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(w / 2, h - 140, "Certificate of Achievement")
    c.setFont("Helvetica", 13)
    c.drawCentredString(w / 2, h - 175, cert.title)
    c.setFont("Helvetica", 15)
    name = f"{cert.trainer.first_name} {cert.trainer.last_name or ''}".strip()
    c.drawCentredString(w / 2, h - 230, f"Presented to {name} ({cert.trainer.trainer_id})")
    c.setFont("Helvetica", 12)
    c.drawCentredString(
        w / 2, h - 260,
        f"For {cert.sessions_completed} completed sessions "
        f"({cert.period_from} to {cert.period_to})")
    if cert.description:
        c.setFont("Helvetica-Oblique", 11)
        c.drawCentredString(w / 2, h - 285, cert.description[:120])
    c.setFont("Helvetica", 11)
    c.drawCentredString(w / 2, 120, f"Issued {cert.issued_at.date() if cert.issued_at else ''}")
    c.save()
    return buf.getvalue()


class TrainerCertificateViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainerCertificate.objects.select_related("trainer").all()
    serializer_class = TrainerCertificateSerializer
    perm_module = "coaches"
    list_perm = "coaches.view"
    retrieve_perm = "coaches.view"
    create_perm = "coaches.manage"
    edit_perm = "coaches.manage"
    destroy_perm = "coaches.manage"
    permission_classes = [HasPerm("coaches.manage")]
    filterset_fields = ["trainer"]

    def get_permissions(self):
        # A trainer reads back their OWN certificates (self-scoped in
        # get_queryset below); issuing/editing/deleting stays admin-only.
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        qs = scope_queryset(super().get_queryset(), self.request.user, "trainer__school_id")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser:
            if getattr(getattr(user, "role", None), "name", None) == "coach":
                if user.coach_profile_id:
                    return qs.filter(trainer_id=user.coach_profile_id)
                return qs.none()
            # Any other (non-admin) account still needs an explicit grant.
            if not (user.has_perm_code("coaches.view") or user.has_perm_code("coaches.manage")):
                return qs.none()
        return qs

    def perform_create(self, serializer):
        obj = serializer.save(issued_by=self.request.user)
        log_audit(self.request, "coach.certificate_issue", "coaches", f"cert:{obj.pk}",
                  new={"trainer": obj.trainer_id, "title": obj.title})

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("coaches.manage")])
    def generate(self, request):
        """Issue a certificate for completed sessions in a period (default: last 3 months).

        Body: {trainer, period_from?, period_to?, title?, description?}.
        The session count snapshots at issue; a PDF is generated and attached.
        """
        from datetime import date as d
        from django.core.files.base import ContentFile
        from apps.sessions.models import Session
        trainer_id = request.data.get("trainer")
        if not trainer_id:
            return Response({"detail": "trainer is required."}, status=400)
        from apps.common.scope import scope_queryset
        coach = scope_queryset(Coach.objects.all(), request.user, "school_id").filter(pk=trainer_id).first()
        if not coach:
            return Response({"detail": "Trainer not found."}, status=404)
        to_date = d.fromisoformat(request.data["period_to"]) if request.data.get("period_to") else d.today()
        frm = request.data.get("period_from")
        if frm:
            period_from = d.fromisoformat(frm)
        else:
            # Default: the last 3 calendar months including the current one.
            from datetime import timedelta
            first = to_date.replace(day=1)
            period_from = (first - timedelta(days=1)).replace(day=1)
            period_from = (period_from - timedelta(days=1)).replace(day=1)
        count = Session.objects.filter(coach=coach, status="completed",
                                       date__gte=period_from, date__lte=to_date).count()
        cert = TrainerCertificate.objects.create(
            trainer=coach,
            title=request.data.get("title") or "Certificate of Service Excellence",
            period_from=period_from, period_to=to_date,
            sessions_completed=count,
            description=request.data.get("description", ""),
            issued_by=request.user)
        pdf = _render_certificate_pdf(cert)
        fname = f"certificate_{coach.trainer_id}_{period_from}_{to_date}.pdf"
        cert.file.save(fname, ContentFile(pdf))
        log_audit(request, "coach.certificate_issue", "coaches", f"cert:{cert.pk}",
                  new={"trainer": coach.pk, "sessions": count})
        return Response(self.get_serializer(cert).data, status=201)


class CoachSportViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = CoachSport.objects.select_related("coach", "sport").all()
    serializer_class = CoachSportSerializer
    perm_module = "coaches"
    permission_classes = [HasPerm("coaches.manage")]
    filterset_fields = ["coach", "sport"]


class CoachAvailabilityViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = CoachAvailability.objects.select_related("coach").all()
    serializer_class = CoachAvailabilitySerializer
    perm_module = "coaches"
    permission_classes = [HasPerm("coaches.manage")]
    filterset_fields = ["coach", "weekday"]


class CoachAssignmentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = CoachAssignment.objects.select_related("coach", "batch").all()
    serializer_class = CoachAssignmentSerializer
    perm_module = "coaches"
    permission_classes = [HasPerm("coaches.manage")]
    filterset_fields = ["coach", "batch"]
