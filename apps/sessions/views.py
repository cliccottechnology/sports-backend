from datetime import date, datetime, timedelta

from django.db import connection, transaction
from django_filters import rest_framework as filters
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from . import services
from .models import Session, SessionStudent


class SessionStudentSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.first_name", read_only=True)
    student_ref = serializers.CharField(source="student.student_id", read_only=True)

    class Meta:
        model = SessionStudent
        fields = "__all__"


class SessionSerializer(serializers.ModelSerializer):
    assigned = SessionStudentSerializer(many=True, read_only=True)
    batch_name = serializers.CharField(source="batch.name", read_only=True)
    sport_name = serializers.CharField(source="sport.name", read_only=True)
    location_name = serializers.SerializerMethodField()
    coach_name = serializers.SerializerMethodField()
    makeup_for_date = serializers.DateField(source="makeup_for.date", read_only=True)

    class Meta:
        model = Session
        fields = "__all__"

    def get_location_name(self, obj):
        return obj.location.name if obj.location_id else ""

    def get_coach_name(self, obj):
        return f"{obj.coach.first_name} {obj.coach.last_name}".strip() if obj.coach_id else ""

    def validate(self, attrs):
        sport = attrs.get("sport") or getattr(self.instance, "sport", None)
        coach = attrs.get("coach") or getattr(self.instance, "coach", None)
        day = attrs.get("date") or getattr(self.instance, "date", None)
        start = attrs.get("start_time") or getattr(self.instance, "start_time", None)
        end = attrs.get("end_time") or getattr(self.instance, "end_time", None)
        # Rules below only run when scheduling is touched (create, or an edit to
        # sport/coach/date/time) so legacy rows never block unrelated edits.
        scheduling = self.instance is None or bool(
            {"sport", "coach", "date", "start_time", "end_time"} & set(attrs.keys()))

        if sport and not sport.is_active:
            raise serializers.ValidationError("Inactive sports cannot be used for sessions.")

        if scheduling and start and end and end <= start:
            raise serializers.ValidationError("End time must be after the start time.")

        off = services.week_off_conflict(coach, day)
        if off:
            raise serializers.ValidationError(off)

        # academy-wide week off (Settings → academy.week_off_days, Mon=0..Sun=6).
        # Enforced only when scheduling (create or date change) so existing
        # rows never block unrelated edits. An explicit allow_off_day flag
        # (admin-confirmed exceptional scheduling, e.g. holiday camp) bypasses.
        if day and (not self.instance or "date" in attrs):
            req = self.context.get("request")
            override = False
            try:
                override = bool(req and req.data.get("allow_off_day"))
            except Exception:
                override = False
            if not override:
                try:
                    from apps.academy.models import get_setting
                    batch = attrs.get("batch") or getattr(self.instance, "batch", None)
                    school_id = getattr(batch, "school_id", None)
                    offs = get_setting("academy.week_off_days", school_id, []) or []
                    if day.weekday() in [int(x) for x in offs]:
                        raise serializers.ValidationError(
                            f"Academy week off ({day.strftime('%A')}). Remove the day in Settings to schedule.")
                except serializers.ValidationError:
                    raise
                except Exception:
                    pass  # settings table unavailable → skip this check

        if scheduling:
            qual = services.qualification_conflict(coach, sport)
            if qual:
                raise serializers.ValidationError(qual)
            avail = services.availability_conflict(coach, day, start, end)
            if avail:
                raise serializers.ValidationError(avail)

        # conflict: same coach overlapping same date (any sport/batch/location),
        # honouring the configured break buffer.
        if coach and day and start and end:
            conflicts = services.find_conflicts(
                coach, day, start, end,
                exclude_pk=self.instance.pk if self.instance else None)
            if conflicts:
                raise serializers.ValidationError(
                    services.conflict_message(coach, conflicts[0], day))

        # academy-wide daily slots: only one session per (date, slot).
        # Enforced only when scheduling (create or date/slot change) so legacy
        # rows never block unrelated edits.
        if not self.instance or "slot" in attrs or "date" in attrs:
            slot = attrs.get("slot")
            if slot is None:
                slot = getattr(self.instance, "slot", 1) if self.instance else 1
            if day and slot:
                # Slots are per academy: school A scheduling Session 1 must not
                # block school B on the same day.
                batch = attrs.get("batch") or getattr(self.instance, "batch", None)
                school_id = getattr(batch, "school_id", None)
                qs = Session.objects.filter(date=day, slot=slot)
                if school_id is not None:
                    qs = qs.filter(batch__school_id=school_id)
                if self.instance:
                    qs = qs.exclude(pk=self.instance.pk)
                if qs.exists():
                    raise serializers.ValidationError(
                        f"Session {slot} is already scheduled for {day}. The academy runs 3 sessions per day.")

        # Make-up sessions: link to the missed session they replace. Validation
        # keeps the traceability honest — same sport, on/after the original date,
        # and never a link to itself.
        is_makeup = attrs.get("is_makeup", getattr(self.instance, "is_makeup", False))
        makeup_for = attrs.get("makeup_for", getattr(self.instance, "makeup_for", None))
        if makeup_for is not None:
            is_makeup = True
            attrs["is_makeup"] = True
            if self.instance is not None and makeup_for.pk == self.instance.pk:
                raise serializers.ValidationError({"makeup_for": "A make-up session cannot replace itself."})
            if sport and makeup_for.sport_id and makeup_for.sport_id != sport.pk:
                raise serializers.ValidationError(
                    {"makeup_for": "A make-up session must be for the same sport as the missed session."})
            if day and makeup_for.date and day < makeup_for.date:
                raise serializers.ValidationError(
                    {"makeup_for": "A make-up session cannot be scheduled before the missed session."})
            if makeup_for.status == "cancelled":
                raise serializers.ValidationError(
                    {"makeup_for": "Cannot build a make-up on a cancelled session."})
        elif is_makeup and self.instance is None:
            raise serializers.ValidationError(
                {"makeup_for": "Select the missed session this make-up replaces."})
        return attrs


class SessionFilter(filters.FilterSet):
    student = filters.NumberFilter(field_name="assigned__student", label="Assigned student pk")
    date__gte = filters.DateFilter(field_name="date", lookup_expr="gte")
    date__lte = filters.DateFilter(field_name="date", lookup_expr="lte")

    class Meta:
        model = Session
        fields = ["sport", "batch", "coach", "date", "status", "slot",
                  "student", "date__gte", "date__lte"]


def _lock_coach(coach):
    """Serialize concurrent allocations for the same trainer (row lock)."""
    if coach is None:
        return
    from apps.coaches.models import Coach
    Coach.objects.select_for_update().filter(pk=coach.pk).first()


def _lock_day(day):
    """Serialize all allocations for a calendar day so the academy-wide slot
    rule cannot be double-booked by concurrent requests (PostgreSQL only)."""
    if not day or connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [day.toordinal()])


class SessionViewSet(GranularPermissions, viewsets.ModelViewSet):
    serializer_class = SessionSerializer
    perm_module = "sessions"
    extra_action_perms = {"my_sessions": "sessions.view", "today": "sessions.view",
                          "change_status": "sessions.edit",
                          "available_coaches": "sessions.view",
                          "suggest_slots": "sessions.view",
                          "timetable": "sessions.view"}
    filterset_class = SessionFilter
    search_fields = ["batch__name", "notes"]
    ordering_fields = ["date", "start_time", "slot"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = Session.objects.select_related("sport", "batch", "coach", "location").prefetch_related("assigned__student").all().order_by("-date")
        user = self.request.user
        # Trainers see only assigned sessions
        if user.is_authenticated and not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if not user.coach_profile_id:
                from apps.coaches.models import Coach
                coach = (
                    Coach.objects.filter(login_account=user).first()
                    or Coach.objects.filter(email__iexact=user.email, email__gt="").first()
                    or Coach.objects.filter(phone=user.phone, phone__gt="").first()
                    or Coach.objects.filter(trainer_id__iexact=user.username).first()
                    or Coach.objects.filter(first_name__iexact=user.first_name, last_name__iexact=user.last_name).first()
                )
                if not coach and Coach.objects.count() == 1:
                    coach = Coach.objects.first()
                if coach:
                    user.coach_profile = coach
                    user.save(update_fields=["coach_profile"])
            if user.coach_profile_id:
                qs = qs.filter(coach_id=user.coach_profile_id)
            else:
                qs = qs.none()
        qs = scope_to_parent(qs, user, "assigned__student_id")
        return scope_queryset(qs, user, "batch__school_id").distinct()

    # NOTE: GranularPermissions.get_permissions decides everything, including
    # the my_sessions / today / change_status / suggest_slots / timetable actions.

    def get_permissions(self):
        # add_student is the make-up roster gate: an admin (sessions.edit /
        # attendance.correct) may always act; a trainer (attendance.mark) may
        # only self-serve when the academy does NOT require make-up approval.
        # validate_for_fee is a money action → trainer_fees.verify.
        action = getattr(self, "action", None)
        if action == "add_student":
            from apps.common.permissions import HasAnyPerm
            return [HasAnyPerm("sessions.edit", "attendance.correct", "attendance.mark")]
        if action == "validate_for_fee":
            return [HasPerm("trainer_fees.verify")]
        if action == "change_status":
            from rest_framework.permissions import IsAuthenticated
            return [IsAuthenticated()]
        return super().get_permissions()

    def _resolved(self, serializer, field):
        data = serializer.validated_data
        if field in data:
            return data[field]
        return getattr(serializer.instance, field, None) if serializer.instance is not None else None

    def _assert_schedulable(self, serializer, creating):
        """Authoritative re-check performed under row locks (concurrency-safe)."""
        coach = self._resolved(serializer, "coach")
        day = self._resolved(serializer, "date")
        start = self._resolved(serializer, "start_time")
        end = self._resolved(serializer, "end_time")
        exclude = serializer.instance.pk if serializer.instance is not None else None
        if coach and day and start and end:
            conflicts = services.find_conflicts(coach, day, start, end, exclude_pk=exclude)
            if conflicts:
                raise DRFValidationError(services.conflict_message(coach, conflicts[0], day))
        changed = set(serializer.validated_data.keys())
        if creating or "slot" in changed or "date" in changed:
            slot = self._resolved(serializer, "slot") or 1
            if day and slot:
                batch = self._resolved(serializer, "batch")
                school_id = getattr(batch, "school_id", None)
                qs = Session.objects.filter(date=day, slot=slot)
                if school_id is not None:
                    qs = qs.filter(batch__school_id=school_id)
                if exclude:
                    qs = qs.exclude(pk=exclude)
                if qs.exists():
                    raise DRFValidationError(
                        f"Session {slot} is already scheduled for {day}. The academy runs 3 sessions per day.")

    def perform_create(self, serializer):
        day = serializer.validated_data.get("date")
        coach = serializer.validated_data.get("coach")
        with transaction.atomic():
            _lock_day(day)
            _lock_coach(coach)
            self._assert_schedulable(serializer, creating=True)
            session = serializer.save()
            # snapshot roster from batch
            roster = session.batch.rosters.filter(is_active=True).values_list("student_id", flat=True)
            SessionStudent.objects.bulk_create(
                [SessionStudent(session=session, student_id=sid) for sid in roster],
                ignore_conflicts=True,
            )
            log_audit(self.request, "session.create", "sessions", f"session:{session.pk}",
                      new={"batch": session.batch.name, "date": str(session.date),
                           "off_day_override": bool(self.request.data.get("allow_off_day"))})

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "batch" in serializer.validated_data:
            ensure_school(self.request, getattr(serializer.validated_data.get("batch"), "school_id", None),
                          field_name="batch")
        if "coach" in serializer.validated_data:
            ensure_school(self.request, getattr(serializer.validated_data.get("coach"), "school_id", None),
                          field_name="coach")
        inst = serializer.instance
        day = serializer.validated_data.get("date", inst.date)
        coach = serializer.validated_data.get("coach", inst.coach)
        old = {"date": str(inst.date), "start_time": str(inst.start_time),
               "end_time": str(inst.end_time), "coach": inst.coach_id, "status": inst.status}
        with transaction.atomic():
            _lock_day(day)
            _lock_coach(coach)
            self._assert_schedulable(serializer, creating=False)
            session = serializer.save()
            log_audit(self.request, "session.update", "sessions", f"session:{session.pk}", old=old,
                      new={"date": str(session.date), "start_time": str(session.start_time),
                           "end_time": str(session.end_time), "coach": session.coach_id,
                           "status": session.status})

    @action(detail=False, methods=["get"])
    def available_coaches(self, request):
        """Eligible alternative coaches for a sport + date + time window.

        Filters: sport (pk, required), date (YYYY-MM-DD, required),
        start_time, end_time (HH:MM, required), exclude_session (pk, optional).
        A coach qualifies when: active, qualified for the sport (CoachSport),
        not on weekly off that weekday, and holding no overlapping live session
        (any sport). Academy scoping applies.
        """
        from apps.coaches.models import Coach, CoachSport
        from apps.common.scope import user_school_id
        sport_id = request.query_params.get("sport")
        date_raw = request.query_params.get("date")
        start_raw = request.query_params.get("start_time")
        end_raw = request.query_params.get("end_time")
        if not (sport_id and date_raw and start_raw and end_raw):
            return Response(
                {"detail": "sport, date, start_time and end_time are required."}, status=400)
        try:
            day = datetime.strptime(date_raw, "%Y-%m-%d").date()
            st = datetime.strptime(start_raw, "%H:%M").time()
            en = datetime.strptime(end_raw, "%H:%M").time()
        except ValueError:
            return Response({"detail": "Use date YYYY-MM-DD and times HH:MM."}, status=400)
        if en <= st:
            return Response({"detail": "end_time must be after start_time."}, status=400)
        sid = user_school_id(request.user)
        buffer_minutes = services.get_break_minutes(sid)
        coaches = Coach.objects.filter(is_active=True)
        if sid is not None and hasattr(Coach, "school_id"):
            coaches = coaches.filter(school_id=sid)
        qualified = set(CoachSport.objects.filter(sport_id=sport_id).values_list("coach_id", flat=True))
        exclude = request.query_params.get("exclude_session") or 0
        busy = set()
        for s in Session.objects.filter(
                date=day, status__in=services.LIVE_STATUSES).exclude(pk=exclude).select_related("coach"):
            if services.overlaps(day, st, en, s.start_time, s.end_time, buffer_minutes):
                busy.add(s.coach_id)
        # Configured availability windows: when a coach has
        # CoachAvailability rows for this weekday, the requested
        # window must sit fully inside one of them. Coaches with
        # no rows configured are unconstrained by windows.
        window_ok = set()
        for c in coaches:
            if c.pk in busy:
                continue
            windows = services.availability_windows(c, day)
            if not windows:
                window_ok.add(c.pk)
                continue
            for w in windows:
                if w.start_time <= st and en <= w.end_time:
                    window_ok.add(c.pk)
                    break
        out = []
        for c in coaches:
            if c.pk not in qualified:
                continue
            if (c.weekly_off_days or []) and day.weekday() in list(c.weekly_off_days):
                continue
            if c.pk in busy or c.pk not in window_ok:
                continue
            out.append({"id": c.pk, "trainer_id": c.trainer_id,
                        "name": f"{c.first_name} {c.last_name or ''}".strip()})
        return Response({"date": date_raw, "available": out, "count": len(out)})

    @action(detail=False, methods=["get"])
    def suggest_slots(self, request):
        """Next free slots for a coach on a date, within their working hours.

        Params: coach, date, duration (minutes) — or start_time + end_time to
        derive the duration. Optional: exclude_session (skip while editing)."""
        from apps.coaches.models import Coach
        coach_id = request.query_params.get("coach")
        day = services.parse_date(request.query_params.get("date"))
        duration = request.query_params.get("duration")
        if not duration:
            st = services.parse_time(request.query_params.get("start_time"))
            en = services.parse_time(request.query_params.get("end_time"))
            if st and en and en > st and day:
                duration = (datetime.combine(day, en) - datetime.combine(day, st)).seconds // 60
        try:
            duration = int(duration) if duration else 0
        except (TypeError, ValueError):
            duration = 0
        if not (coach_id and day and duration > 0):
            return Response({"detail": "coach, date and a positive duration (minutes) are required."}, status=400)
        from apps.common.scope import user_school_id
        coach_qs = Coach.objects.filter(pk=coach_id)
        sid = user_school_id(request.user)
        if sid is not None:
            coach_qs = coach_qs.filter(school_id=sid)
        coach = coach_qs.first()
        if not coach:
            return Response({"detail": "Coach not found."}, status=404)
        exclude = request.query_params.get("exclude_session") or None
        slots = services.suggest_slots(coach, day, duration, exclude_pk=exclude)
        return Response({"date": day.isoformat(), "coach": coach.pk,
                         "duration": duration, "slots": slots})

    def _timetable_session(self, s, flagged):
        return {
            "id": s.pk, "slot": s.slot,
            "sport": s.sport_id, "sport_name": s.sport.name if s.sport_id else "",
            "batch": s.batch_id, "batch_name": s.batch.name if s.batch_id else "",
            "coach": s.coach_id,
            "coach_name": f"{s.coach.first_name} {s.coach.last_name or ''}".strip() if s.coach_id else "",
            "location_name": s.location.name if s.location_id else "",
            "start_time": s.start_time.strftime("%H:%M") if s.start_time else "",
            "end_time": s.end_time.strftime("%H:%M") if s.end_time else "",
            "status": s.status,
            "has_conflict": s.pk in flagged,
        }

    @action(detail=False, methods=["get"])
    def timetable(self, request):
        """Daily/weekly/monthly source: sessions for a date range (scoped).

        Params: date_from, date_to (YYYY-MM-DD; default today), optional coach,
        optional sport. Returns per-day sessions, academy off-days, and — when a
        coach is selected — their weekly off and availability windows. Sessions
        that overlap another live session for the same coach are flagged."""
        date_from = services.parse_date(request.query_params.get("date_from")) or date.today()
        date_to = services.parse_date(request.query_params.get("date_to")) or date_from
        if date_to < date_from:
            date_from, date_to = date_to, date_from
        if (date_to - date_from).days > 62:
            date_to = date_from + timedelta(days=62)
        coach_id = request.query_params.get("coach") or None
        sport_id = request.query_params.get("sport") or None
        base = self.get_queryset().filter(date__gte=date_from, date__lte=date_to)
        all_rows = list(base.order_by("date", "start_time"))
        by_day = {}
        for s in all_rows:
            by_day.setdefault(s.date, []).append(s)
        flagged = set()
        for lst in by_day.values():
            flagged |= services.day_conflict_ids(lst)
        rows = all_rows
        if coach_id:
            rows = [s for s in rows if str(s.coach_id) == str(coach_id)]
        if sport_id:
            rows = [s for s in rows if str(s.sport_id) == str(sport_id)]
        try:
            from apps.academy.models import get_setting
            from apps.common.scope import user_school_id
            academy_offs = [int(x) for x in (get_setting(
                "academy.week_off_days", user_school_id(request.user), []) or [])]
        except Exception:
            academy_offs = []
        coach = None
        if coach_id:
            from apps.coaches.models import Coach
            from apps.common.scope import user_school_id
            coach_qs = Coach.objects.filter(pk=coach_id)
            sid = user_school_id(request.user)
            if sid is not None:
                coach_qs = coach_qs.filter(school_id=sid)
            coach = coach_qs.first()
        days = []
        cur = date_from
        while cur <= date_to:
            entry = {
                "date": cur.isoformat(),
                "weekday": cur.weekday(),
                "is_academy_off": cur.weekday() in academy_offs,
                "sessions": [self._timetable_session(s, flagged) for s in rows if s.date == cur],
            }
            if coach:
                entry["coach_off"] = cur.weekday() in list(coach.weekly_off_days or [])
                entry["windows"] = [
                    {"start_time": w.start_time.strftime("%H:%M"), "end_time": w.end_time.strftime("%H:%M")}
                    for w in services.availability_windows(coach, cur)
                ]
            days.append(entry)
            cur += timedelta(days=1)
        return Response({"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "days": days})

    @action(detail=False, methods=["get"])
    def my_sessions(self, request):
        qs = self.get_queryset()
        date_ = request.query_params.get("date")
        if date_:
            qs = qs.filter(date=date_)
        page = self.paginate_queryset(qs)
        ser = self.get_serializer(page or qs, many=True)
        return self.get_paginated_response(ser.data) if page is not None else Response(ser.data)

    @action(detail=False, methods=["get"])
    def today(self, request):
        from datetime import date as d
        qs = self.get_queryset().filter(date=d.today())
        ser = self.get_serializer(qs, many=True)
        return Response(ser.data)

    @action(detail=True, methods=["post"])
    def change_status(self, request, pk=None):
        from rest_framework.exceptions import PermissionDenied
        session = self.get_object()
        user = request.user
        new_status = request.data.get("status")
        if new_status not in dict([("scheduled", ""), ("in_progress", ""), ("completed", ""), ("cancelled", "")]):
            return Response({"detail": "Invalid status."}, status=400)

        # Admins may set any status. Assigned coach may update between scheduled, in_progress, and completed.
        is_assigned_coach = (
            user.is_authenticated
            and getattr(getattr(user, "role", None), "name", None) == "coach"
            and user.coach_profile_id == session.coach_id
        )
        can_manage = user.is_superuser or user.has_perm_code("sessions.manage") or user.has_perm_code("sessions.edit")
        if not can_manage:
            if not is_assigned_coach:
                raise PermissionDenied("You can only change status for your own assigned sessions.")
            if new_status == "cancelled":
                raise PermissionDenied("Only administrators can cancel a session.")
        # Re-check conflicts when a cancelled session is being revived, since
        # its slot/time may already be taken by another live session.
        if new_status in services.LIVE_STATUSES and session.status not in services.LIVE_STATUSES:
            conflicts = services.find_conflicts(
                session.coach, session.date, session.start_time, session.end_time,
                exclude_pk=session.pk)
            if conflicts:
                return Response(
                    {"detail": services.conflict_message(session.coach, conflicts[0], session.date)},
                    status=400)
            # The day's slot may have been taken while this session was cancelled.
            school_id = session.batch.school_id if session.batch_id else None
            slot_qs = Session.objects.filter(date=session.date, slot=session.slot).exclude(pk=session.pk)
            if school_id is not None:
                slot_qs = slot_qs.filter(batch__school_id=school_id)
            if slot_qs.exists():
                return Response(
                    {"detail": f"Session {session.slot} is already scheduled for {session.date}."},
                    status=400)
        old = session.status
        session.status = new_status
        session.save(update_fields=["status"])
        log_audit(request, "session.status", "sessions", f"session:{session.pk}", old={"status": old}, new={"status": new_status})

        if new_status == "completed" and old != "completed":
            try:
                from apps.notifications.utils import admin_recipients, notify_users
                school_id = session.batch.school_id if session.batch_id else None
                admins = admin_recipients(school_id=school_id)
                if admins:
                    coach_name = user.get_full_name() or user.username
                    batch_name = session.batch.name if session.batch else f"Session #{session.pk}"
                    title = f"Session Completed: {batch_name}"
                    message = f"Coach {coach_name} marked {batch_name} on {session.date} as completed."
                    notify_users(admins, title, message)
            except Exception:
                pass

        return Response(self.get_serializer(session).data)

    @action(detail=True, methods=["post"])
    def add_student(self, request, pk=None):
        """Add a student to a session's roster (authorized make-up attendance).

        Body: ``{student, makeup_for?}`` where ``makeup_for`` is the missed
        session pk. Enforces school scope, sport enrollment, batch capacity,
        duplicate prevention and the academy's make-up approval policy
        (Settings → attendance.makeup_requires_approval). When a make-up link
        is supplied the session becomes traceable back to the missed session;
        the original absence is left untouched and no salary/fee is raised here.
        """
        from apps.academy.models import get_setting
        from apps.common.scope import user_school_id
        from apps.students.models import Student

        session = self.get_object()
        if session.status == "cancelled":
            return Response({"detail": "Cannot add students to a cancelled session."}, status=400)
        try:
            student = Student.objects.get(pk=request.data.get("student"))
        except (Student.DoesNotExist, TypeError, ValueError):
            return Response({"detail": "Student not found."}, status=404)

        user = request.user
        sid = user_school_id(user)
        if sid is not None and (session.batch.school_id != sid or student.school_id != sid):
            return Response({"detail": "Outside your school's scope."}, status=403)

        makeup_for = None
        makeup_for_id = request.data.get("makeup_for")
        if makeup_for_id:
            makeup_for = Session.objects.filter(pk=makeup_for_id).first()
            if not makeup_for:
                return Response({"detail": "Missed session not found."}, status=404)
            if makeup_for.pk == session.pk:
                return Response({"detail": "A session cannot be its own make-up."}, status=400)
            if makeup_for.sport_id != session.sport_id:
                return Response({"detail": "A make-up must be for the same sport as the missed session."}, status=400)
            if session.date < makeup_for.date:
                return Response({"detail": "A make-up cannot be scheduled before the missed session."}, status=400)

        requires_approval = bool(get_setting(
            "attendance.makeup_requires_approval", session.batch.school_id, True))
        can_approve = (user.is_superuser or user.has_perm_code("sessions.edit")
                       or user.has_perm_code("attendance.correct"))
        if makeup_for and requires_approval and not can_approve:
            return Response(
                {"detail": "Make-up attendance requires administrator approval. "
                           "Ask an admin to add this student to the make-up session."},
                status=403)

        if student.sport_id != session.sport_id:
            return Response(
                {"detail": f"{student.first_name} is not enrolled in {session.sport.name}."}, status=400)
        if SessionStudent.objects.filter(session=session, student=student).exists():
            return Response({"detail": "Student is already on this session's roster."}, status=400)
        capacity = session.batch.capacity
        if capacity and SessionStudent.objects.filter(session=session).count() >= capacity:
            return Response({"detail": "Batch capacity reached for this session."}, status=400)

        link = SessionStudent.objects.create(session=session, student=student)
        if makeup_for and not session.makeup_for_id:
            # Keep the make-up session traceable to the missed one at session level
            # too (per-student traceability lives on the attendance record).
            session.is_makeup = True
            session.makeup_for = makeup_for
            session.save(update_fields=["is_makeup", "makeup_for"])
        log_audit(request, "session.add_student", "sessions", f"session:{session.pk}",
                  new={"student": student.pk, "makeup_for": getattr(makeup_for, "pk", None)})
        return Response({"session": session.pk, "student": student.pk,
                         "makeup_for": getattr(makeup_for, "pk", None)}, status=201)

    @action(detail=True, methods=["post"])
    def validate_for_fee(self, request, pk=None):
        """Admin validation controlling whether a session credits trainer salary.

        Required for make-up sessions and, when the academy enables
        ``trainer.require_session_validation``, for every session."""
        session = self.get_object()
        raw = request.data.get("validated", True)
        validated = raw if isinstance(raw, bool) else str(raw).lower() in ("1", "true", "yes", "on")
        old = session.validated_for_trainer_fee
        session.validated_for_trainer_fee = validated
        session.save(update_fields=["validated_for_trainer_fee"])
        log_audit(request, "session.validate_fee", "sessions", f"session:{session.pk}",
                  old={"validated": old}, new={"validated": validated})
        return Response({"session": session.pk, "validated_for_trainer_fee": validated})
