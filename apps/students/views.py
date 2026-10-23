from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

import django_filters
from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasAnyPerm, HasPerm
from .models import EnrollmentHistory, Student, StudentCertificate, StudentDocument, StudentReferencePhoto


def _cert_policy(school_id=None):
    """Academy certificate rules (Settings → Student Certificates).

    Duration-only by default; thresholds apply only when configured. Resolved
    per academy: an academy override wins, otherwise the platform default
    applies — so one academy's thresholds never leak into another.
    """
    from apps.academy.models import get_setting

    def num(key, default=None):
        try:
            v = get_setting(key, school_id, None)
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    raw_active = get_setting("certificate.require_active", school_id, True)
    return {
        "months": int(num("certificate.min_months", 3) or 3),
        "min_attendance_pct": num("certificate.min_attendance_pct"),
        "min_sessions": int(num("certificate.min_sessions", 0) or 0),
        "require_active": raw_active not in (False, "false", "False", "0", 0),
    }


def _add_months(d, months):
    m = d.month - 1 + months
    import calendar
    return d.replace(year=d.year + m // 12, month=m % 12 + 1,
                     day=min(d.day, calendar.monthrange(d.year + m // 12, m % 12 + 1)[1]))


def _eligibility_row(student, policy, today):
    from datetime import timedelta
    from apps.attendance.models import AttendanceRecord
    from apps.sessions.models import SessionStudent
    start = student.enrollment_date
    milestone = _add_months(start, policy["months"]) if start else None
    window_end = min(milestone, today) if milestone else today
    recs = AttendanceRecord.objects.filter(student=student)
    if start:
        recs = recs.filter(session__date__gte=start, session__date__lte=window_end)
    marked = recs.exclude(status="not_marked").count()
    present = recs.filter(status="present").count()
    att_pct = round(100 * present / marked, 1) if marked else None
    sess_done = SessionStudent.objects.filter(
        student=student, session__status="completed")
    if start:
        sess_done = sess_done.filter(session__date__gte=start, session__date__lte=window_end)
    sess_done = sess_done.count()
    checks = {
        "active": (not policy["require_active"]) or student.status == "active",
        "attendance": (policy["min_attendance_pct"] is None
                       or (att_pct is not None and att_pct >= policy["min_attendance_pct"])),
        "sessions": sess_done >= policy["min_sessions"],
    }
    if milestone is None:
        state = "no_enrollment_date"
    elif today < milestone - timedelta(days=30):
        state = "training"
    elif today < milestone:
        state = "approaching"
    else:
        state = "eligible" if all(checks.values()) else "review"
    issued = None
    if milestone:
        issued = (StudentCertificate.objects
                  .filter(student=student, sport=student.sport_id, period_from=start)
                  .order_by("-issued_at").first())
    if issued:
        state = "issued"
    return {
        "student": student.pk, "student_id": student.student_id,
        "name": f"{student.first_name} {student.last_name or ''}".strip(),
        "sport": student.sport.name if student.sport_id else None,
        "enrollment_date": str(start) if start else None,
        "milestone_date": str(milestone) if milestone else None,
        "state": state, "checks": checks,
        "attendance_pct": att_pct, "sessions_completed": sess_done,
        "certificate": ({"id": issued.pk, "certificate_no": issued.certificate_no,
                         "file": issued.file.url if issued.file else None}
                        if issued else None),
    }


class StudentDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentDocument
        fields = "__all__"
        read_only_fields = ["uploaded_by", "uploaded_at"]

    def validate_file(self, f):
        from django.conf import settings
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower()
        if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
            raise serializers.ValidationError(f"File type .{ext} not allowed.")
        if f.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise serializers.ValidationError("File too large.")
        return f


class EnrollmentHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = EnrollmentHistory
        fields = "__all__"


class StudentSerializer(serializers.ModelSerializer):
    documents = StudentDocumentSerializer(many=True, read_only=True)
    sport_name = serializers.CharField(source="sport.name", read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    reference_photos = serializers.SerializerMethodField()

    class Meta:
        model = Student
        fields = "__all__"

    def get_reference_photos(self, obj):
        photos = obj.reference_photos.filter(is_active=True).order_by("created_at")
        req = self.context.get("request")
        def url(p):
            try:
                u = p.image.url if p.image else None
            except ValueError:
                u = None
            if u and req:
                return req.build_absolute_uri(u)
            return u
        return [{"id": p.pk, "image": url(p), "angle": p.angle,
                 "consent_given": p.consent_given} for p in photos]

    def validate(self, attrs):
        sport = attrs.get("sport") or getattr(self.instance, "sport", None)
        if sport and not sport.is_active and not self.instance:
            raise serializers.ValidationError("Inactive sports cannot be assigned to new students.")
        photo = attrs.get("photo")
        if photo:
            from django.conf import settings
            ext = (photo.name.rsplit(".", 1)[-1] if "." in photo.name else "").lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                raise serializers.ValidationError({"photo": "Photo must be JPG, PNG, or WEBP."})
            if photo.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
                raise serializers.ValidationError({"photo": "Photo too large (max 10 MB)."})
        return attrs


class StudentFilter(django_filters.FilterSet):
    batch = django_filters.NumberFilter(field_name="batch_links__batch", distinct=True)

    class Meta:
        model = Student
        fields = ["status", "sport", "gender", "batch", "school"]


class StudentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Student.objects.select_related("sport", "school").prefetch_related("documents").all().order_by("-created_at")
    serializer_class = StudentSerializer
    perm_module = "students"
    extra_action_perms = {"summary": "students.view"}
    filterset_class = StudentFilter
    search_fields = ["student_id", "first_name", "last_name", "phone", "email"]
    ordering_fields = ["student_id", "enrollment_date", "created_at", "first_name"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = scope_queryset(super().get_queryset(), self.request.user, "school_id")
        return scope_to_parent(qs, self.request.user, "pk")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Live header counts: total + per-status (respects no filters by design)."""
        from django.db.models import Count
        from apps.common.scope import scope_queryset, scope_to_parent
        qs = scope_to_parent(scope_queryset(Student.objects.all(), request.user, "school_id"),
                             request.user, "pk")
        by_status = {r["status"]: r["n"] for r in qs.values("status").annotate(n=Count("id"))}
        return Response({"total": qs.count(), "by_status": by_status})

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        given = serializer.validated_data.pop("school", None)
        obj = serializer.save(school_id=ensure_school(self.request, given))
        log_audit(self.request, "student.create", "students", f"student:{obj.pk}", new={"student_id": obj.student_id})

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "school" in serializer.validated_data:
            ensure_school(self.request, serializer.validated_data.get("school"))
        old_status = self.get_object().status
        obj = serializer.save()
        log_audit(self.request, "student.update", "students", f"student:{obj.pk}",
                  old={"status": old_status}, new={"status": obj.status})


class StudentDocumentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = StudentDocument.objects.select_related("student").all()
    serializer_class = StudentDocumentSerializer
    perm_module = "students"
    permission_classes = [HasPerm("students.manage")]
    filterset_fields = ["student"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "student__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        student = serializer.validated_data.get("student")
        ensure_school(self.request, getattr(student, "school_id", None), field_name="student")
        serializer.save(uploaded_by=self.request.user)


class StudentReferencePhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentReferencePhoto
        fields = "__all__"
        read_only_fields = ["uploaded_by", "created_at"]

    def validate_image(self, f):
        from django.conf import settings
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower()
        if ext not in ("jpg", "jpeg", "png", "webp"):
            raise serializers.ValidationError("Reference photo must be JPG, PNG, or WEBP.")
        if f.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise serializers.ValidationError("File too large.")
        return f

    def validate(self, attrs):
        if attrs.get("consent_given") and not attrs.get("consented_by"):
            raise serializers.ValidationError("consented_by is required when consent is recorded.")
        return attrs


class StudentReferencePhotoViewSet(viewsets.ModelViewSet):
    """Enroll/manage consented reference photos for face matching.

    Multiple ACTIVE photos per student are allowed — one per angle — so AI
    attendance can match athletes from different angles. Cap: 5 active photos
    per student; deactivate (don't delete) to preserve audit history.

    Coaches may list/retrieve (to see enrollment status); only users with
    students.manage may update/deactivate.
    """
    queryset = StudentReferencePhoto.objects.select_related("student").all().order_by("-created_at")
    serializer_class = StudentReferencePhotoSerializer
    module = "students"
    filterset_fields = ["student", "is_active", "consent_given", "angle"]

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        if self.action == "create":
            # Managers (students.manage) or coaches enrolling their own athletes.
            return [IsAuthenticated()]
        return [HasPerm("students.manage")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        return scope_to_parent(
            scope_queryset(super().get_queryset(), self.request.user, "student__school_id"),
            self.request.user, "student_id")

    def perform_create(self, serializer):
        from django.utils import timezone
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from .models import StudentReferencePhoto
        user = self.request.user
        student = serializer.validated_data["student"]
        if not user.has_perm_code("students.manage"):
            if not (user.has_perm_code("attendance.mark") and self._is_own_athlete(user, student.pk)):
                raise PermissionDenied("You may only enroll reference photos for your own athletes.")
        # Multi-angle enrollment: keep existing active photos; cap the total.
        active = StudentReferencePhoto.objects.filter(student=student, is_active=True).count()
        if serializer.validated_data.get("is_active", True) and active >= StudentReferencePhoto.MAX_ACTIVE_PER_STUDENT:
            raise ValidationError(f"At most {StudentReferencePhoto.MAX_ACTIVE_PER_STUDENT} active reference photos per student. Deactivate one first.")
        # New enrollments are always active. (HTML form posts omit unchecked
        # booleans, which DRF parses as False — never trust the posted value here.)
        obj = serializer.save(uploaded_by=self.request.user, is_active=True)
        if obj.consent_given and not obj.consented_at:
            obj.consented_at = timezone.now()
            obj.save(update_fields=["consented_at"])
        if obj.consent_given and obj.is_active:
            try:
                from faceauth.enrollment import index_student_faces
                index_student_faces([student.pk], student.school_id)
            except Exception:
                pass
        log_audit(self.request, "student.refphoto_enroll", "students", f"student:{student.pk}",
                  new={"photo": obj.pk, "consent": obj.consent_given})

    def _is_own_athlete(self, user, student_pk: int) -> bool:
        """True when the student is assigned to one of the coach's sessions/batches."""
        coach_id = getattr(user, "coach_profile_id", None)
        if not coach_id:
            return False
        from apps.batches.models import BatchStudent
        from apps.sessions.models import SessionStudent
        if SessionStudent.objects.filter(session__coach_id=coach_id, student_id=student_pk).exists():
            return True
        return BatchStudent.objects.filter(batch__coach_id=coach_id, student_id=student_pk,
                                           is_active=True).exists()


class StudentCertificateSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source="student.first_name", read_only=True)
    sport_name = serializers.CharField(source="sport.name", read_only=True)

    class Meta:
        model = StudentCertificate
        fields = "__all__"
        read_only_fields = ["attendance_pct", "sessions_completed", "certificate_no",
                            "issued_by", "issued_at"]


def _render_student_certificate_pdf(cert, academy_name):
    """Premium institute certificate. Returns bytes (reportlab)."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(w / 2, h - 120, academy_name)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(w / 2, h - 155, cert.title)
    name = f"{cert.student.first_name} {cert.student.last_name or ''}".strip()
    c.setFont("Helvetica", 14)
    c.drawCentredString(w / 2, h - 210, f"Presented to {name} ({cert.student.student_id})")
    sport = cert.sport.name if cert.sport_id else "Sports Training"
    c.drawCentredString(w / 2, h - 235, f"Sport: {sport}")
    c.setFont("Helvetica", 12)
    c.drawCentredString(w / 2, h - 260,
                        f"Training completed {cert.period_from} to {cert.period_to}")
    if cert.attendance_pct is not None:
        c.drawCentredString(w / 2, h - 280, f"Attendance: {cert.attendance_pct}% · "
                                            f"Sessions: {cert.sessions_completed}")
    else:
        c.drawCentredString(w / 2, h - 280, f"Sessions completed: {cert.sessions_completed}")
    c.setFont("Helvetica", 11)
    c.drawCentredString(w / 2, 130, f"Certificate No: {cert.certificate_no or '—'}")
    c.drawCentredString(w / 2, 112, f"Issued: {cert.issued_at.date() if cert.issued_at else ''}")
    if cert.description:
        c.setFont("Helvetica-Oblique", 10)
        c.drawCentredString(w / 2, 94, cert.description[:130])
    c.save()
    return buf.getvalue()


class StudentCertificateViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = StudentCertificate.objects.select_related("student", "sport").all()
    serializer_class = StudentCertificateSerializer
    perm_module = "students"
    list_perm = "students.view"
    retrieve_perm = "students.view"
    create_perm = "students.edit"
    edit_perm = "students.edit"
    destroy_perm = "students.edit"
    permission_classes = [HasPerm("students.manage")]
    filterset_fields = ["student", "sport"]
    extra_action_perms = {"eligibility": "students.view", "issue": "students.edit"}

    def get_queryset(self):
        from apps.common.scope import scope_queryset, scope_to_parent
        return scope_to_parent(
            scope_queryset(super().get_queryset(), self.request.user, "student__school_id"),
            self.request.user, "student_id")

    @action(detail=False, methods=["get"])
    def eligibility(self, request):
        """Milestone dashboard: approaching / eligible / issued per student.

        Query: ?state=eligible (optional), ?sport= (optional).
        Only active-policy checks run; unapproved students are never issued here.
        """
        from datetime import date as d
        from apps.common.scope import scope_queryset, scope_to_parent, user_school_id
        today = d.today()
        qs = scope_to_parent(scope_queryset(
            Student.objects.select_related("sport").all(), request.user, "school_id"),
            request.user, "pk")
        sport = request.query_params.get("sport")
        if sport:
            qs = qs.filter(sport_id=sport)
        # Policy is per academy, so resolve it lazily per school (a super admin
        # may span multiple academies) rather than applying one school's rules
        # to everyone.
        policy_cache: dict = {}

        def policy_for(school_id):
            if school_id not in policy_cache:
                policy_cache[school_id] = _cert_policy(school_id)
            return policy_cache[school_id]

        rows = [_eligibility_row(s, policy_for(s.school_id), today) for s in qs[:500]]
        state = request.query_params.get("state")
        if state:
            rows = [r for r in rows if r["state"] == state]
        counts: dict = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return Response({"policy": _cert_policy(user_school_id(request.user)),
                         "counts": counts, "results": rows, "today": str(today)})

    @action(detail=False, methods=["post"])
    def issue(self, request):
        """Approve + issue: recompute eligibility, snapshot, PDF, notify parents."""
        from datetime import date as d
        from django.core.files.base import ContentFile
        student_id = request.data.get("student")
        if not student_id:
            return Response({"detail": "student is required."}, status=400)
        from apps.common.scope import scope_queryset, scope_to_parent
        student = scope_to_parent(
            scope_queryset(Student.objects.select_related("sport").all(), request.user, "school_id"),
            request.user, "pk").filter(pk=student_id).first()
        if not student:
            return Response({"detail": "Student not found."}, status=404)
        policy = _cert_policy(student.school_id)
        row = _eligibility_row(student, policy, d.today())
        if row["state"] not in ("eligible", "review"):
            return Response({"detail": "Student is not eligible for a certificate.",
                             "state": row["state"], "checks": row["checks"]}, status=400)
        if row["certificate"]:
            return Response({"detail": "A certificate already exists for this milestone. "
                             "Delete it explicitly to re-issue.", "certificate": row["certificate"]},
                            status=400)
        cert = StudentCertificate.objects.create(
            student=student, sport=student.sport,
            title=request.data.get("title") or "Certificate of Training Completion",
            period_from=student.enrollment_date, period_to=row["milestone_date"],
            milestone_date=row["milestone_date"],
            attendance_pct=row["attendance_pct"], sessions_completed=row["sessions_completed"],
            description=request.data.get("description", ""),
            issued_by=request.user)
        try:
            from apps.academy.models import get_setting
            academy_name = get_setting("academy.name", student.school_id) or "HARI Sports Academy"
        except Exception:
            academy_name = "HARI Sports Academy"
        pdf = _render_student_certificate_pdf(cert, academy_name)
        cert.file.save(f"{cert.certificate_no or f'CERT-{cert.pk}'}.pdf", ContentFile(pdf))
        log_audit(request, "student.certificate_issue", "students", f"cert:{cert.pk}",
                  new={"student": student.pk, "sessions": cert.sessions_completed})
        notified = _notify_parents_of_certificate(request, student, cert)
        return Response({**self.get_serializer(cert).data, "notified": notified}, status=201)


def _notify_parents_of_certificate(request, student, cert):
    """In-app inbox for linked parent accounts + WhatsApp where configured.

    Returns a small delivery summary. Never fails the issuance itself.
    """
    from apps.accounts.models import User
    from apps.parents.models import Parent, StudentParent
    from apps.notifications.models import Notification
    notified = {"inbox": 0, "whatsapp": 0, "skipped": 0}
    try:
        parent_ids = list(StudentParent.objects.filter(student=student).values_list("parent_id", flat=True))
        users = User.objects.filter(is_active=True, parent_profile_id__in=parent_ids)
        title = "Certificate issued"
        message = (f"Congratulations! {student.first_name} completed training "
                   f"({cert.period_from} to {cert.period_to}) and received: {cert.title} "
                   f"({cert.certificate_no}).")
        Notification.objects.bulk_create([Notification(user=u, title=title, message=message) for u in users])
        notified["inbox"] = len(users)
        try:
            from apps.whatsapp.views import _deliver
            from apps.whatsapp.models import WhatsAppMessage
            for p in Parent.objects.filter(pk__in=parent_ids).exclude(mobile=""):
                pref = (p.communication_preference or "whatsapp").lower()
                if pref not in ("whatsapp", ""):
                    notified["skipped"] += 1
                    continue
                num = "".join(ch for ch in str(p.mobile or p.whatsapp or "") if ch.isdigit())[-10:]
                if not num:
                    notified["skipped"] += 1
                    continue
                try:
                    provider_name, result = _deliver(to_number=num, body=f"{title}\n{message}")
                    WhatsAppMessage.objects.create(
                        to_number=num, body=f"{title}\n{message}", related_student=student,
                        provider=provider_name or "mock",
                        provider_message_id=(getattr(result, "provider_message_id", "") or ""),
                        status=(getattr(result, "status", "") or "queued"),
                        failure_info=(getattr(result, "failure_info", "") or ""),
                        created_by=request.user)
                    notified["whatsapp"] += 1
                except Exception:
                    notified["skipped"] += 1
        except Exception:
            pass
    except Exception:
        pass
    return notified
