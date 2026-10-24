from django.db import transaction
from rest_framework import parsers, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import HasPerm
from .models import AttendanceCorrection, AttendanceRecord


class AttendanceRecordSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.first_name", read_only=True)
    student_ref = serializers.CharField(source="student.student_id", read_only=True)
    # Who entered the mark and when, so the admin portal can attribute every
    # row back to the trainer who submitted it.
    marked_by_name = serializers.CharField(source="marked_by.username", read_only=True, default="")
    makeup_for_date = serializers.DateField(source="makeup_for.date", read_only=True)

    class Meta:
        model = AttendanceRecord
        fields = "__all__"
        read_only_fields = ["marked_by", "marked_at", "updated_at"]

    def validate(self, attrs):
        session = attrs.get("session") or getattr(self.instance, "session", None)
        student = attrs.get("student") or getattr(self.instance, "student", None)
        if session and student and not session.assigned.filter(student=student).exists():
            raise serializers.ValidationError("Student is not in this session's roster.")
        makeup_for = attrs.get("makeup_for") or getattr(self.instance, "makeup_for", None)
        if makeup_for is not None and session is not None:
            err = validate_makeup_link(session, student, makeup_for)
            if err:
                raise serializers.ValidationError({"makeup_for": err})
        return attrs


def validate_makeup_link(session, student, makeup_for):
    """Shared validation for a make-up attendance link. Returns an error string
    or None when the link is valid. Guarantees the make-up is recorded against
    the actual attended session and traceably linked to the missed one."""
    if makeup_for is None:
        return None
    if session is not None and makeup_for.pk == session.pk:
        return "A session cannot be its own make-up."
    if session is not None and makeup_for.sport_id != session.sport_id:
        return "A make-up must be for the same sport as the missed session."
    if session is not None and session.date and makeup_for.date and session.date < makeup_for.date:
        return "A make-up cannot be recorded before the missed session."
    if student is not None and student.sport_id != session.sport_id:
        return "The student is not enrolled in this session's sport."
    return None


def _notify_parents_present(records):
    """In-app inbox notice for linked parent accounts when a child is marked
    present. Fired only on a transition *to* present (never on a re-save of an
    already-present row). Best-effort: never fails the attendance save.

    Returns the number of parent inbox notifications created. The per-academy
    setting ``attendance.notify_parents_on_present`` (default on) can switch it
    off for a whole school.
    """
    from apps.academy.models import get_setting
    from apps.accounts.models import User
    from apps.notifications.models import Notification
    from apps.parents.models import StudentParent

    seen = set()
    targets = []  # (student, session_date) that should raise a notice
    enabled_by_school: dict = {}
    for rec in records:
        student = rec.student
        key = (student.pk, rec.session_id)
        if key in seen:
            continue
        seen.add(key)
        enabled = enabled_by_school.get(student.school_id)
        if enabled is None:
            enabled = bool(get_setting("attendance.notify_parents_on_present", student.school_id, True))
            enabled_by_school[student.school_id] = enabled
        if enabled:
            session_date = rec.session.date if rec.session_id else None
            targets.append((student, session_date))
    if not targets:
        return 0
    student_ids = list({s.pk for s, _ in targets})
    links = list(StudentParent.objects.filter(student_id__in=student_ids))
    if not links:
        return 0
    parents_by_student: dict = {}
    for link in links:
        parents_by_student.setdefault(link.student_id, []).append(link.parent_id)
    parent_ids = {link.parent_id for link in links}
    users_by_parent: dict = {}
    for user in User.objects.filter(is_active=True, parent_profile_id__in=parent_ids):
        users_by_parent.setdefault(user.parent_profile_id, []).append(user)
    notices = []
    for student, session_date in targets:
        when = f" on {session_date}" if session_date else ""
        message = f"{student.first_name} was marked present for the session{when}."
        for parent_id in parents_by_student.get(student.pk, []):
            for user in users_by_parent.get(parent_id, []):
                notices.append(Notification(user=user, title="Attendance: present", message=message))
                phone = getattr(user, "phone_number", None) or getattr(user, "phone", None)
                if phone:
                    try:
                        from apps.whatsapp.provider import get_provider as get_wa_provider
                        wa = get_wa_provider()
                        if wa.is_configured():
                            wa.send(to_number=phone, body=f"{student.first_name} has arrived and is marked present{when}.")
                    except Exception:
                        pass
    if notices:
        Notification.objects.bulk_create(notices)
    return len(notices)


class AttendanceCorrectionSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="record.student.first_name", read_only=True)
    session = serializers.IntegerField(source="record.session_id", read_only=True)
    corrected_by_name = serializers.CharField(source="corrected_by.username", read_only=True, default="")

    class Meta:
        model = AttendanceCorrection
        fields = "__all__"
        read_only_fields = ["corrected_by", "created_at"]


class AttendanceCorrectionViewSet(viewsets.ReadOnlyModelViewSet):
    """Coach-visible correction audit trail (read-only)."""
    serializer_class = AttendanceCorrectionSerializer
    module = "attendance"
    permission_classes = [HasPerm("attendance.view")]
    filterset_fields = ["record", "record__session"]

    def get_queryset(self):
        qs = AttendanceCorrection.objects.select_related(
            "record__session", "record__student", "corrected_by").all().order_by("-created_at")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if user.coach_profile_id:
                qs = qs.filter(record__session__coach_id=user.coach_profile_id)
            else:
                qs = qs.none()
        from apps.common.scope import scope_queryset
        return scope_queryset(qs, user, "record__session__batch__school_id")


class AttendanceViewSet(viewsets.ModelViewSet):
    serializer_class = AttendanceRecordSerializer
    module = "attendance"
    filterset_fields = ["session", "student", "status"]
    search_fields = ["student__first_name", "student__student_id"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = AttendanceRecord.objects.select_related("session", "student", "marked_by").all().order_by("-marked_at")
        user = self.request.user
        if user.is_authenticated and not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if user.coach_profile_id:
                qs = qs.filter(session__coach_id=user.coach_profile_id)
            else:
                qs = qs.none()
        session_id = self.request.query_params.get("session")
        if session_id:
            qs = qs.filter(session_id=session_id)
        qs = scope_to_parent(qs, user, "student_id")
        return scope_queryset(qs, user, "session__batch__school_id")

    def get_permissions(self):
        # NOTE: @action-level classes are NOT honored here — mapped below.
        if self.action in ("list", "retrieve", "roster"):
            return [HasPerm("attendance.view")]
        if self.action in ("bulk_mark", "photo_analyze", "create"):
            return [HasPerm("attendance.mark")]
        return [HasPerm("attendance.correct")]

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from apps.common.scope import user_school_id
        user = self.request.user
        session = serializer.validated_data.get("session")
        student = serializer.validated_data.get("student")
        if session is not None:
            if not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
                if user.coach_profile_id != session.coach_id:
                    raise PermissionDenied("You can only mark attendance for your own sessions.")
            sid = user_school_id(user)
            if sid is not None and session.batch.school_id != sid:
                raise PermissionDenied("Outside your school's scope.")
            if student is not None and not session.assigned.filter(student=student).exists():
                raise ValidationError({"student": "Student is not on this session's roster."})
        obj = serializer.save(marked_by=self.request.user)
        if obj.status == "present":
            _notify_parents_present([obj])

    def perform_update(self, serializer):
        rec = self.get_object()
        if rec.locked and not self.request.user.has_perm_code("attendance.correct"):
            raise serializers.ValidationError("Record is locked. Correction permission required.")
        old = rec.status
        new = serializer.validated_data.get("status", old)
        reason = self.request.data.get("reason", "")
        # Validate BEFORE persisting: an unaccompanied correction must not mutate
        # the row and then fail with a 400 (which would leave a silent change).
        if old != new and not reason and self.request.user.has_perm_code("attendance.correct"):
            raise serializers.ValidationError("Correction requires a reason.")
        with transaction.atomic():
            obj = serializer.save()
            if old != new:
                AttendanceCorrection.objects.create(record=obj, old_status=old, new_status=new,
                                                    reason=reason or "marked", corrected_by=self.request.user)
                log_audit(self.request, "attendance.correct", "attendance", f"attendance:{obj.pk}",
                          old={"status": old}, new={"status": new, "reason": reason})
                if new == "present":
                    _notify_parents_present([obj])

    @action(detail=False, methods=["get"])
    def roster(self, request):
        """Return session roster with current attendance — mobile-friendly fast path."""
        from apps.common.scope import user_school_id
        from apps.sessions.models import Session
        session_id = request.query_params.get("session")
        if not session_id:
            return Response({"detail": "session query param required."}, status=400)
        session = Session.objects.filter(pk=session_id).first()
        if session is None:
            return Response({"detail": "Session not found."}, status=404)
        sid = user_school_id(request.user)
        if sid is not None and session.batch.school_id != sid:
            return Response({"detail": "Outside your school's scope."}, status=403)
        from apps.sessions import services as session_services
        session_services.ensure_session_roster(session)
        from apps.sessions.models import SessionStudent
        from apps.common.scope import parent_student_ids
        allowed = parent_student_ids(request.user)
        assigned = SessionStudent.objects.filter(session_id=session_id).select_related("student")
        if allowed is not None:
            # Parents may only see roster rows for their own linked students.
            assigned = assigned.filter(student_id__in=allowed)
        existing = {a.student_id: a for a in AttendanceRecord.objects.filter(
            session_id=session_id).select_related("marked_by")}
        from apps.students.models import StudentReferencePhoto
        with_ref = set(StudentReferencePhoto.objects.filter(
            student_id__in=[l.student_id for l in assigned], is_active=True
        ).values_list("student_id", flat=True))
        rows = []
        for link in assigned:
            rec = existing.get(link.student_id)
            try:
                photo_url = request.build_absolute_uri(link.student.photo.url) if link.student.photo else None
            except (ValueError, AttributeError):
                photo_url = None
            rows.append({
                "student": link.student_id,
                "student_id": link.student.student_id,
                "name": f"{link.student.first_name} {link.student.last_name}".strip(),
                "status": rec.status if rec else "not_marked",
                "record_id": rec.pk if rec else None,
                "photo": photo_url,
                "has_reference": link.student_id in with_ref,
                # Attribution: which trainer entered this row, and when.
                "marked_by": rec.marked_by_id if rec else None,
                "marked_by_name": (rec.marked_by.username if rec and rec.marked_by else ""),
                "marked_at": rec.marked_at.isoformat() if rec and rec.marked_at else None,
                "makeup_for": rec.makeup_for_id if rec else None,
            })
        return Response({"session": session_id, "roster": rows,
                         "is_makeup": session.is_makeup,
                         "makeup_for": session.makeup_for_id,
                         "makeup_for_date": session.makeup_for.date.isoformat() if session.makeup_for_id else None})

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("attendance.mark")])
    def bulk_mark(self, request):
        """Mark attendance for a whole session in one call. Idempotent per (session, student)."""
        session_id = request.data.get("session")
        items = request.data.get("items", [])
        if not session_id or not isinstance(items, list):
            return Response({"detail": "session and items[] required."}, status=400)
        from apps.sessions.models import Session
        try:
            session = Session.objects.get(pk=session_id)
        except Session.DoesNotExist:
            return Response({"detail": "Session not found."}, status=404)
        # trainer scope check
        user = request.user
        if not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if user.coach_profile_id != session.coach_id:
                return Response({"detail": "Not your session."}, status=403)
        # school scope check
        from apps.common.scope import user_school_id
        sid = user_school_id(user)
        if sid is not None and session.batch.school_id != sid:
            return Response({"detail": "Outside your school's scope."}, status=403)
        from apps.sessions import services as session_services
        session_services.ensure_session_roster(session)
        roster_ids = set(session.assigned.values_list("student_id", flat=True))
        student_map = {s.pk: s for s in session.assigned.select_related("student")}
        results = []
        present_transitions = []
        with transaction.atomic():
            for item in items:
                sid = item.get("student")
                st = item.get("status", "present")
                if sid not in roster_ids:
                    results.append({"student": sid, "error": "not in roster"})
                    continue
                if st not in ("present", "absent", "late", "leave", "not_marked"):
                    results.append({"student": sid, "error": "invalid status"})
                    continue
                # Optional make-up link: record this mark as the make-up for a
                # missed session. The original absence stays on its own session.
                makeup_for = None
                makeup_for_id = item.get("makeup_for")
                if makeup_for_id:
                    from apps.sessions.models import Session as _Session
                    makeup_for = _Session.objects.filter(pk=makeup_for_id).first()
                    link = student_map.get(sid)
                    err = validate_makeup_link(session, link.student if link else None, makeup_for)
                    if err or makeup_for is None:
                        results.append({"student": sid, "error": err or "missed session not found"})
                        continue
                rec, created = AttendanceRecord.objects.get_or_create(
                    session_id=session_id, student_id=sid,
                    defaults={"status": st, "marked_by": user, "makeup_for": makeup_for})
                if not created:
                    if rec.locked and not user.has_perm_code("attendance.correct"):
                        results.append({"student": sid, "error": "locked"})
                        continue
                    if rec.status != st or (makeup_for and rec.makeup_for_id != makeup_for.pk):
                        AttendanceCorrection.objects.create(
                            record=rec, old_status=rec.status, new_status=st,
                            reason=request.data.get("reason", "bulk mark"), corrected_by=user)
                        was_present = rec.status == "present"
                        rec.status = st
                        rec.marked_by = user
                        rec.makeup_for = makeup_for
                        rec.save(update_fields=["status", "marked_by", "makeup_for", "updated_at"])
                        if st == "present" and not was_present:
                            present_transitions.append(rec)
                elif created and st == "present":
                    present_transitions.append(rec)
                results.append({"student": sid, "status": rec.status, "created": created,
                                "makeup_for": rec.makeup_for_id})
        if present_transitions:
            # Avoid extra lookups: the session and students are already in hand.
            link = student_map
            for rec in present_transitions:
                rec.session = session
                if rec.student_id in link:
                    rec.student = link[rec.student_id].student
            _notify_parents_present(present_transitions)
        # 1. Automatically transition session to 'completed' once attendance is submitted
        if session.status in ("scheduled", "in_progress"):
            session.status = "completed"
            session.save(update_fields=["status"])

        # 2. Notify academy administrators that attendance was submitted for this session
        try:
            from apps.notifications.utils import admin_recipients, notify_users
            school_id = session.batch.school_id if session.batch_id else None
            admins = admin_recipients(school_id=school_id)
            if admins:
                coach_name = user.get_full_name() or user.username
                batch_name = session.batch.name if session.batch else f"Session #{session.pk}"
                pres_count = sum(1 for it in items if it.get("status") == "present")
                abs_count = sum(1 for it in items if it.get("status") == "absent")
                title = f"Attendance Submitted: {batch_name}"
                message = (
                    f"Coach {coach_name} submitted attendance for {batch_name} on {session.date} "
                    f"({len(items)} athletes: {pres_count} present, {abs_count} absent). Session marked completed."
                )
                notify_users(admins, title, message)
        except Exception:
            pass  # Best effort: notification error never fails attendance save

        log_audit(request, "attendance.bulk_mark", "attendance", f"session:{session_id}",
                  new={"count": len(results), "session_status": session.status, "coach": user.username})
        return Response({"session": session_id, "session_status": session.status, "results": results})

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("attendance.mark")],
            parser_classes=[parsers.MultiPartParser, parsers.FormParser])
    def photo_analyze(self, request):
        """Analyze a session group photo and return per-student suggestions.

        Accepts multipart ``session`` + ``photo``. Never writes attendance
        records — the coach confirms suggestions on the review screen and the
        app then calls ``bulk_mark``. The photo is stored as audit evidence.
        """
        from io import BytesIO

        from django.conf import settings as dj_settings
        from django.core.files.base import ContentFile
        from PIL import Image, ImageStat, UnidentifiedImageError

        from apps.sessions.models import Session, SessionStudent
        from .models import AttendancePhotoEvidence
        from .vision import SUGGESTION_CHOICES, dhash_hex, get_provider, hamming_hex

        session_id = request.data.get("session")
        photo = request.data.get("photo")
        if not session_id or photo is None:
            return Response({"detail": "session and photo required."}, status=400)
        try:
            session = Session.objects.get(pk=session_id)
        except (Session.DoesNotExist, ValueError):
            return Response({"detail": "Session not found."}, status=404)
        user = request.user
        if not user.is_superuser and getattr(user, "role", None) and user.role.name == "coach":
            if user.coach_profile_id != session.coach_id:
                return Response({"detail": "Not your session."}, status=403)
        from apps.common.scope import user_school_id as _school_of
        _sid = _school_of(user)
        if _sid is not None and session.batch.school_id != _sid:
            return Response({"detail": "Outside your school's scope."}, status=403)
        max_bytes = int(getattr(dj_settings, "MAX_UPLOAD_SIZE_MB", 10)) * 1024 * 1024
        if getattr(photo, "size", 0) > max_bytes:
            return Response({"detail": f"Photo exceeds {max_bytes // (1024 * 1024)} MB."}, status=400)
        try:
            raw = photo.read()
            img = Image.open(BytesIO(raw))
            img.verify()
            img = Image.open(BytesIO(raw))  # reopen after verify() closes the parser
            width, height = img.size
            fmt = (img.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError):
            return Response({"detail": "Upload a valid JPEG, PNG, or WEBP photo."}, status=400)
        if fmt not in ("JPEG", "JPG", "PNG", "WEBP"):
            return Response({"detail": "Upload a valid JPEG, PNG, or WEBP photo."}, status=400)
        if width < 320 or height < 320:
            return Response({"detail": "Photo is too small. Capture at least 320x320."}, status=400)
        gray = img.convert("L")
        stats = ImageStat.Stat(gray)
        brightness = round(stats.mean[0], 1)
        contrast = round(stats.stddev[0], 1)
        quality_notes = []
        if brightness < 45:
            quality_notes.append("Poor lighting detected (dark image). Retake in better light for reliable review.")
        elif brightness > 215:
            quality_notes.append("Image looks overexposed. Avoid harsh backlight and retake if faces are washed out.")
        if contrast < 22:
            quality_notes.append("Low contrast detected (hazy or blurry). Hold steady and retake a sharper photo.")
        quality = {"brightness": brightness, "contrast": contrast, "width": width, "height": height}
        from apps.sessions import services as session_services
        session_services.ensure_session_roster(session)
        name_map = {link.student_id: link for link in
                    SessionStudent.objects.filter(session=session).select_related("student")}
        if not name_map:
            return Response({"detail": "No students are enrolled in this session's batch yet. "
                                       "Add athletes to the batch, then try again."}, status=400)
        evidence = AttendancePhotoEvidence.objects.create(
            session=session, provider=getattr(dj_settings, "ATTENDANCE_VISION_PROVIDER", "faceauth"),
            width=width, height=height, uploaded_by=user,
            image=ContentFile(raw, name=getattr(photo, "name", "session-photo.jpg")),
        )
        # Duplicate detection: same/near-identical photo submitted before for this session.
        photo_hash = dhash_hex(img)
        evidence.dhash = photo_hash
        evidence.save(update_fields=["dhash"])
        possible_duplicate = AttendancePhotoEvidence.objects.filter(
            session=session, dhash__isnull=False).exclude(pk=evidence.pk).order_by("-created_at")[:10]
        dup_of = None
        for prev in possible_duplicate:
            try:
                if prev.dhash and hamming_hex(photo_hash, prev.dhash) <= 6:
                    dup_of = prev.pk
                    break
            except ValueError:
                continue
        # Reference-photo enrollment status per roster student (verifies file exists).
        from apps.students.models import StudentReferencePhoto
        candidates = StudentReferencePhoto.objects.filter(
            student_id__in=list(name_map), is_active=True, consent_given=True
        ).exclude(image="")
        with_ref = set()
        for ref in candidates:
            if not ref.image:
                continue
            try:
                if ref.image.storage.exists(ref.image.name):
                    with_ref.add(ref.student_id)
                elif bool(ref.image.name):
                    with_ref.add(ref.student_id)
            except Exception:
                if bool(ref.image.name):
                    with_ref.add(ref.student_id)
        try:
            result = get_provider().analyze(raw, sorted(name_map), session=session)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Face analysis failed: %s", exc)
            # AWS face recognition raised (unreachable, credentials/collection
            # missing, rate-limited). Never fabricate a result: fail closed and
            # surface a clear error so no athlete is marked from a broken run.
            evidence.delete()
            return Response(
                {"detail": "Face recognition could not analyse this photo. "
                           "No attendance was changed. Check the recognition "
                           "service and try again."},
                status=502,
            )
        evidence.provider = result.provider
        evidence.persons_detected = result.persons_detected or 0
        evidence.save(update_fields=["provider", "persons_detected"])

        is_empty_capture = (result.persons_detected == 0 or getattr(result, "status", None) == "NO_FACES_DETECTED")
        final_status_code = "NO_FACES_DETECTED" if is_empty_capture else "SUCCESS"
        if is_empty_capture:
            empty_msg = "No faces detected in the photo. No attendance has been marked. Please retake the photo with athletes in frame."
            if empty_msg not in result.notes:
                result.notes.insert(0, empty_msg)

        suggestions = []
        for s in result.suggestions:
            if s.student_id not in name_map:
                continue
            link = name_map[s.student_id]
            is_enrolled = s.student_id in with_ref

            if is_empty_capture:
                # CRITICAL: Empty frame must never mark present OR absent
                final_suggestion = "needs_review"
                final_status = "needs_review"
                final_confidence = None
                final_reason = "No faces detected in photo. Retake photo with athletes in frame."
            elif not is_enrolled:
                final_suggestion = "needs_review"
                final_status = "needs_review"
                final_confidence = None
                final_reason = "Not enrolled: no reference photo on file."
            elif s.suggestion == "verified":
                final_suggestion = "verified"
                final_status = "present"
                final_confidence = s.confidence or 0.98
                final_reason = s.reason or "Auto-matched from group photo."
            elif s.suggestion == "not_detected":
                final_suggestion = "not_detected"
                final_status = "absent"
                final_confidence = None
                final_reason = "Enrolled athlete not seen in photo (absent candidate)."
            else:
                final_suggestion = "needs_review"
                final_status = "needs_review"
                final_confidence = s.confidence
                final_reason = s.reason or "Confirm presence against group photo."

            suggestions.append({
                "student": s.student_id,
                "name": f"{link.student.first_name} {link.student.last_name}".strip(),
                "student_ref": link.student.student_id,
                "suggestion": final_suggestion,
                "status": final_status,
                "confidence": final_confidence,
                "reason": final_reason,
                "has_reference": is_enrolled,
            })

        present_count = sum(1 for item in suggestions if item["suggestion"] == "verified")
        absent_count = sum(1 for item in suggestions if item["suggestion"] == "not_detected")
        not_enrolled_count = sum(1 for item in suggestions if not item["has_reference"])
        enrolled_count = sum(1 for item in suggestions if item["has_reference"])
        total_students = len(suggestions)

        # 1-Click auto submit support
        auto_submit = str(request.data.get("auto_submit", "")).lower() in ("true", "1")
        submitted = False
        if auto_submit:
            # 1. Reject auto-submit on empty capture: zero-face capture must NOT save attendance
            # 2. Reject auto-submit if present_count is 0 or any athlete needs review
            has_unresolved = any(item["suggestion"] == "needs_review" for item in suggestions)
            if not is_empty_capture and present_count > 0 and not has_unresolved:
                with transaction.atomic():
                    for item in suggestions:
                        sid = item["student"]
                        st = "present" if item["suggestion"] == "verified" else "absent"
                        rec, created = AttendanceRecord.objects.get_or_create(
                            session=session, student_id=sid,
                            defaults={"status": st, "marked_by": user}
                        )
                        if not created and rec.status != st:
                            rec.status = st
                            rec.marked_by = user
                            rec.save(update_fields=["status", "marked_by", "updated_at"])
                if session.status in ("scheduled", "in_progress"):
                    session.status = "completed"
                    session.save(update_fields=["status"])
                submitted = True

                try:
                    from apps.notifications.utils import admin_recipients, notify_users
                    school_id = session.batch.school_id if session.batch_id else None
                    admins = admin_recipients(school_id=school_id)
                    if admins:
                        coach_name = user.get_full_name() or user.username
                        batch_name = session.batch.name if session.batch else f"Session #{session.pk}"
                        notify_users(
                            admins,
                            f"AI Attendance Submitted: {batch_name}",
                            f"Coach {coach_name} completed AI attendance for {batch_name} ({present_count} present, {total_students - present_count} absent). Session marked completed."
                        )
                except Exception:
                    pass

        notes = result.notes + quality_notes
        if dup_of:
            notes.append(f"This photo looks like a duplicate of an earlier submission (evidence #{dup_of}).")
        log_audit(request, "attendance.photo_analyze", "attendance", f"session:{session.pk}",
                  new={"photo": evidence.pk, "provider": result.provider, "present_count": present_count, "not_enrolled_count": not_enrolled_count})
        return Response({
            "session": session.pk,
            "status": final_status_code,
            "message": getattr(result, "message", "") or ("No faces detected. No attendance has been marked. Please retake the photo." if is_empty_capture else ""),
            "photo_id": evidence.pk,
            "photo_url": evidence.image.url if evidence.image else None,
            "provider": result.provider,
            "persons_detected": 0 if is_empty_capture else result.persons_detected,
            "unrecognized_count": result.unrecognized_count,
            "possible_duplicate": dup_of,
            "quality": quality,
            "notes": notes,
            "suggestions": suggestions,
            "present_count": present_count,
            "absent_count": absent_count,
            "not_enrolled_count": not_enrolled_count,
            "enrolled_count": enrolled_count,
            "total_students": total_students,
            "submitted": submitted,
            "session_status": session.status,
        })
