"""Reports & analytics. All numbers come from live DB queries — no fake dashboard values."""
import csv
from datetime import date
from io import BytesIO, StringIO

from django.db.models import Count, F, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.common.permissions import HasPerm
from apps.common.scope import scope_queryset, scope_to_parent


def _dist(qs, field="status", label_field=None):
    """Distribution rows [{label, count, pct}] from a live queryset."""
    rows = list(qs.values(field).annotate(count=Count("id")).order_by(field))
    total = sum(r["count"] for r in rows)
    out = []
    for r in rows:
        key = r[field]
        out.append({
            "label": str(key).replace("_", " ").title() if key else "Unassigned",
            "value": key,
            "count": r["count"],
            "pct": round(100.0 * r["count"] / total, 1) if total else 0.0,
        })
    return out


def _chart_filters(request, *allowed):
    out = {}
    for key in allowed:
        v = request.query_params.get(key)
        if v:
            out[key] = v
    return out


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def charts(request):
    """Pie/donut chart data for the Admin and Super Admin dashboards.

    Every figure is computed from live database records. Attendance
    percentages use MARKED records (present/absent/late/leave) as the
    denominator; "not_marked" rows are reported separately and never
    counted in the percentage base.

    Filters: from/to (YYYY-MM-DD, session or due date), sport, batch,
    coach. School scoping applies for school admins; parents are never
    granted reports.view, so no parent data leaks here.
    """
    from apps.attendance.models import AttendanceRecord
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.fees.models import StudentFee
    from apps.sessions.models import Session
    from apps.students.models import Student, StudentCertificate
    from apps.trainer_fees.models import TrainerPayable
    from apps.whatsapp.models import WhatsAppMessage

    user = request.user
    f = _chart_filters(request, "from", "to", "sport", "batch", "coach")

    # --- Attendance distribution (filterable, school-scoped) ---
    att_qs = scope_queryset(AttendanceRecord.objects.all(), user, "session__batch__school_id")
    if f.get("from"):
        att_qs = att_qs.filter(session__date__gte=f["from"])
    if f.get("to"):
        att_qs = att_qs.filter(session__date__lte=f["to"])
    if f.get("sport"):
        att_qs = att_qs.filter(session__sport_id=f["sport"])
    if f.get("batch"):
        att_qs = att_qs.filter(session__batch_id=f["batch"])
    if f.get("coach"):
        att_qs = att_qs.filter(session__coach_id=f["coach"])
    marked = att_qs.exclude(status="not_marked")
    marked_total = marked.count()
    att_rows = []
    for st in ("present", "absent", "late", "leave"):
        n = marked.filter(status=st).count()
        att_rows.append({
            "label": st.title(), "value": st, "count": n,
            "pct": round(100.0 * n / marked_total, 1) if marked_total else 0.0,
        })
    not_marked = att_qs.filter(status="not_marked").count()
    attendance_block = {
        "statuses": att_rows,
        "not_marked": {"label": "Not Marked", "value": "not_marked",
                       "count": not_marked, "pct": None},
        "marked_total": marked_total,
        "total_records": att_qs.count(),
        "denominator_note": (
            "Percentages use marked records (present + absent + late + leave) "
            "as the denominator. Not Marked records are excluded from the "
            "percentage base and reported separately."
        ),
    }

    # --- Students by sport / enrollment status (school-scoped) ---
    st_qs = scope_queryset(Student.objects.all(), user, "school_id")
    sport_rows = []
    for r in st_qs.values("sport__name").annotate(count=Count("id")).order_by("sport__name"):
        sport_rows.append({
            "label": r["sport__name"] or "Unassigned", "value": r["sport__name"] or "",
            "count": r["count"],
            "pct": round(100.0 * r["count"] / st_qs.count(), 1) if st_qs.count() else 0.0,
        })

    # --- Sessions by status ---
    se_qs = scope_queryset(Session.objects.all(), user, "batch__school_id")
    if f.get("from"):
        se_qs = se_qs.filter(date__gte=f["from"])
    if f.get("to"):
        se_qs = se_qs.filter(date__lte=f["to"])
    if f.get("sport"):
        se_qs = se_qs.filter(sport_id=f["sport"])
    if f.get("batch"):
        se_qs = se_qs.filter(batch_id=f["batch"])
    if f.get("coach"):
        se_qs = se_qs.filter(coach_id=f["coach"])

    # --- Student fee payment status ---
    fee_qs = scope_queryset(StudentFee.objects.all(), user, "student__school_id")
    if f.get("from"):
        fee_qs = fee_qs.filter(due_date__gte=f["from"])
    if f.get("to"):
        fee_qs = fee_qs.filter(due_date__lte=f["to"])

    # --- Trainer salary payment status + obligations by payment model ---
    tf_qs = scope_queryset(TrainerPayable.objects.all(), user, "trainer__school_id")
    if f.get("from"):
        tf_qs = tf_qs.filter(
            models_Q_or_period(f["from"], None))
    if f.get("to"):
        tf_qs = tf_qs.filter(models_Q_or_period(None, f["to"]))
    tf_status = _dist(tf_qs)
    tf_by_kind = {}
    for kind, label in (("session", "Session-based"), ("weekly_salary", "Weekly salary"),
                        ("monthly_salary", "Monthly salary")):
        kqs = tf_qs.filter(kind=kind)
        obligation = kqs.filter(status__in=["pending", "verified"]).aggregate(t=Sum("amount"))["t"] or 0
        tf_by_kind[kind] = {
            "label": label,
            "count": kqs.count(),
            "obligation": float(obligation),
            "paid": float(kqs.filter(status="paid").aggregate(t=Sum("amount"))["t"] or 0),
            "by_status": _dist(kqs),
        }

    # --- Certificates by sport ---
    cert_qs = scope_queryset(StudentCertificate.objects.all(), user, "student__school_id")
    cert_rows = []
    cert_total = cert_qs.count()
    for r in cert_qs.values("sport__name").annotate(count=Count("id")).order_by("sport__name"):
        cert_rows.append({
            "label": r["sport__name"] or "Unassigned", "value": r["sport__name"] or "",
            "count": r["count"],
            "pct": round(100.0 * r["count"] / cert_total, 1) if cert_total else 0.0,
        })

    # --- Notification delivery status (WhatsApp provider data) ---
    wa_qs = scope_queryset(WhatsAppMessage.objects.all(), user, "related_student__school_id")

    return Response({
        "generated_at": timezone.now().isoformat(),
        "filters": f,
        "attendance": attendance_block,
        "students_by_sport": sport_rows,
        "students_by_status": _dist(st_qs),
        "sessions_by_status": _dist(se_qs),
        "fees_by_status": _dist(fee_qs),
        "trainer_fees": {"by_status": tf_status, "by_kind": tf_by_kind},
        "certificates_by_sport": cert_rows,
        "whatsapp_delivery": _dist(wa_qs, field="status"),
    })


def models_Q_or_period(frm, to):
    """Payables overlapping [frm, to]: dated sessions OR salary periods."""
    from django.db.models import Q
    q = Q()
    if frm:
        q |= Q(session__date__gte=frm) | Q(period_start__gte=frm) | Q(period_end__gte=frm)
    if to:
        q |= Q(session__date__lte=to) | Q(period_start__lte=to) | Q(period_end__lte=to)
    return q


def dashboard_fee_blocks(user):
    """Fee + trainer-fee status distributions for the main dashboard."""
    from apps.fees.models import StudentFee
    from apps.trainer_fees.models import TrainerPayable
    fee_qs = scope_queryset(StudentFee.objects.all(), user, "student__school_id")
    tf_qs = scope_queryset(TrainerPayable.objects.all(), user, "trainer__school_id")
    return _dist(fee_qs), _dist(tf_qs)


def _export(request, rows, filename):
    # NOTE: the download format travels in `?export=` — NOT `?format=`.
    # DRF reserves `format` for content negotiation and 404s unknown
    # renderers before the view runs, so `?format=csv` can never work.
    fmt = request.query_params.get("export") or request.query_params.get("format", "json")
    if fmt not in ("json", ""):
        # Viewing a report needs reports.view; taking a file out needs reports.export.
        user = getattr(request, "user", None)
        allowed = bool(user and user.is_authenticated) and (
            user.is_superuser or user.has_perm_code("reports.export"))
        if not allowed:
            return Response({"detail": "Export requires the Export reports permission."}, status=403)
    if fmt == "csv":
        buf = StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        resp = HttpResponse(buf.getvalue(), content_type="text/csv")
        resp["Content-Disposition"] = f"attachment; filename={filename}.csv"
        return resp
    if fmt == "excel":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        if rows:
            ws.append(list(rows[0].keys()))
            for r in rows:
                ws.append(list(r.values()))
        buf = BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f"attachment; filename={filename}.xlsx"
        return resp
    if fmt == "pdf":
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.drawString(40, 800, filename.replace("_", " ").title())
        y = 770
        for r in rows[:60]:
            c.drawString(40, y, str(r)[:110])
            y -= 14
            if y < 40:
                c.showPage()
                y = 800
        c.save()
        resp = HttpResponse(buf.getvalue(), content_type="application/pdf")
        resp["Content-Disposition"] = f"attachment; filename={filename}.pdf"
        return resp
    return Response({"results": rows, "count": len(rows)})


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def dashboard(request):
    from apps.academy.models import School, Sport
    from apps.attendance.models import AttendanceRecord
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.fees.models import StudentFee
    from apps.payments.models import Payment
    from apps.sessions.models import Session
    from apps.students.models import Student
    from apps.trainer_fees.models import TrainerPayable
    from apps.whatsapp.models import WhatsAppMessage

    today = timezone.localdate()
    stu_qs = scope_queryset(Student.objects.all(), request.user, "school_id")
    students_total = stu_qs.count()
    students_active = stu_qs.filter(status="active").count()
    new_month = stu_qs.filter(enrollment_date__month=today.month, enrollment_date__year=today.year).count()
    per_sport = list(stu_qs.values("sport__name").annotate(count=Count("id")))
    sess_qs = scope_queryset(Session.objects.all(), request.user, "batch__school_id")
    sessions_today = sess_qs.filter(date=today)
    att_today = scope_queryset(AttendanceRecord.objects.all(), request.user,
                               "session__batch__school_id").filter(session__date=today)
    present = att_today.filter(status="present").count()
    absent = att_today.filter(status="absent").count()
    late = att_today.filter(status="late").count()
    leave = att_today.filter(status="leave").count()
    not_marked = att_today.filter(status="not_marked").count()
    total_marked = att_today.exclude(status="not_marked").count()
    fee_qs = scope_queryset(StudentFee.objects.all(), request.user, "student__school_id")
    fees_collected = fee_qs.filter(status="paid").aggregate(t=Sum(F("amount") - F("discount")))["t"] or 0
    fees_pending = fee_qs.exclude(status__in=["paid", "cancelled", "refunded"]).aggregate(t=Sum(F("amount") - F("discount")))["t"] or 0
    fees_overdue = fee_qs.filter(due_date__lt=today).exclude(status__in=["paid", "cancelled", "refunded"]).aggregate(t=Sum(F("amount") - F("discount")))["t"] or 0
    pay_qs = scope_queryset(Payment.objects.all(), request.user, "student_fee__student__school_id")
    pay_ok = pay_qs.filter(status="success").count()
    pay_fail = pay_qs.filter(status="failed").count()
    pay_pend = pay_qs.filter(status__in=["initiated", "pending"]).count()
    tp_qs = scope_queryset(TrainerPayable.objects.all(), request.user, "trainer__school_id")
    tp = tp_qs.aggregate(payable=Sum("amount", filter=models_Q_payable()), paid=Sum("amount", filter=models_Q_paid()))
    wa = scope_queryset(WhatsAppMessage.objects.all(), request.user, "related_student__school_id"
                        ).values("status").annotate(count=Count("id"))
    students_by_status = list(stu_qs.values("status").annotate(count=Count("id")))
    sessions_by_status = list(sess_qs.values("status").annotate(count=Count("id")))
    from apps.students.models import StudentCertificate
    cert_qs = scope_queryset(StudentCertificate.objects.all(), request.user, "student__school_id")
    certs_by_sport = list(cert_qs.values("sport__name").annotate(count=Count("id")))
    coach_qs = scope_queryset(Coach.objects.all(), request.user, "school_id")
    can_see_schools = request.user.is_superuser or getattr(getattr(request.user, "role", None), "name", None) == "super_admin"
    school_rows = []
    if can_see_schools:
        for sch in School.objects.all().order_by("name"):
            school_rows.append({
                "id": sch.pk, "name": sch.name, "active": sch.is_active,
                "students": Student.objects.filter(school=sch).count(),
                "batches": Batch.objects.filter(school=sch).count(),
                "coaches": Coach.objects.filter(school=sch).count(),
                "sessions_today": Session.objects.filter(date=today, batch__school=sch).count(),
            })
    schools_block = {"total": School.objects.count(),
                     "active": School.objects.filter(is_active=True).count(),
                     "schools": school_rows}
    fees_by_status, trainer_fees_by_status = dashboard_fee_blocks(request.user)
    resp = {
        "students": {"total": students_total, "active": students_active, "new_this_month": new_month, "per_sport": per_sport},
        "sports": {"active": Sport.objects.filter(is_active=True).count()},
        "coaches": {"total": coach_qs.count(), "active": coach_qs.filter(is_active=True).count(),
                     "today_sessions": sessions_today.count()},
        "sessions": {"today": sessions_today.count(),
                     "completed": sess_qs.filter(date=today, status="completed").count(),
                     "upcoming": sess_qs.filter(date__gt=today, status="scheduled").count(),
                     "cancelled": sess_qs.filter(date=today, status="cancelled").count()},
        "attendance": {"present": present, "absent": absent, "late": late, "leave": leave,
                       "not_marked": not_marked, "marked": total_marked,
                       "percentage": round(100 * present / total_marked, 1) if total_marked else 0},
        "fees": {"collected": float(fees_collected), "pending": float(fees_pending), "overdue": float(fees_overdue)},
        "payments": {"success": pay_ok, "failed": pay_fail, "pending": pay_pend},
        "trainer_fees": {"payable": float(tp["payable"] or 0), "paid": float(tp["paid"] or 0)},
        "whatsapp": {r["status"]: r["count"] for r in wa},
        "students_by_status": {r["status"]: r["count"] for r in students_by_status},
        "sessions_by_status": {r["status"]: r["count"] for r in sessions_by_status},
        "certificates": {"issued": cert_qs.count(), "by_sport": {
            (r["sport__name"] or "Unassigned"): r["count"] for r in certs_by_sport}},
        "fees_by_status": fees_by_status,
        "trainer_fees_by_status": trainer_fees_by_status,
    }
    # Multi-school overview (onboarded schools, live per-school status) is a
    # platform concern. Academy admins manage their own academy only, so this
    # block is returned to super admins alone — the dashboard hides it otherwise.
    if can_see_schools:
        resp["schools"] = schools_block
    return Response(resp)


def models_Q_active():
    from django.db import models
    return models.Q(rosters__is_active=True)


def models_Q_payable():
    from django.db import models
    return models.Q(status__in=["pending", "verified"])


def models_Q_paid():
    from django.db import models
    return models.Q(status="paid")


def _filtered_students(request):
    from apps.students.models import Student
    from apps.common.scope import scope_queryset, scope_to_parent
    qs = scope_to_parent(
        scope_queryset(Student.objects.select_related("sport").all(), request.user, "school_id"),
        request.user, "pk")
    for key in ("status", "sport", "gender"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    s = request.query_params.get("search")
    if s:
        from django.db.models import Q
        qs = qs.filter(Q(first_name__icontains=s) | Q(student_id__icontains=s) | Q(phone__icontains=s))
    return qs


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def student_master(request):
    rows = list(_filtered_students(request).values("student_id", "first_name", "last_name", "sport__name", "status", "enrollment_date", "phone")[:2000])
    return _export(request, rows, "student_master")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def fee_collection(request):
    from apps.fees.models import StudentFee
    qs = StudentFee.objects.select_related("student", "fee_plan").all()
    qs = scope_queryset(qs, request.user, "student__school_id")
    for key in ("status", "period_label", "student"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    if request.query_params.get("from"):
        qs = qs.filter(due_date__gte=request.query_params["from"])
    if request.query_params.get("to"):
        qs = qs.filter(due_date__lte=request.query_params["to"])
    rows = [{"student": f.student.student_id, "name": f.student.first_name, "plan": f.fee_plan.name,
             "period": f.period_label, "amount": float(f.amount), "status": f.status,
             "paid": float(f.paid_total), "balance": float(f.balance)} for f in qs[:2000]]
    return _export(request, rows, "fee_collection")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def pending_overdue(request):
    from datetime import date as d
    from apps.fees.models import StudentFee
    qs = StudentFee.objects.select_related("student").exclude(status__in=["paid", "cancelled", "refunded"])
    qs = scope_queryset(qs, request.user, "student__school_id")
    if request.query_params.get("overdue_only") == "1":
        qs = qs.filter(due_date__lt=d.today())
    rows = [{"student": f.student.student_id, "due": str(f.due_date), "amount": float(f.amount),
             "balance": float(f.balance), "status": f.status} for f in qs[:2000]]
    return _export(request, rows, "pending_overdue_fees")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def attendance_report(request):
    from apps.attendance.models import AttendanceRecord
    qs = AttendanceRecord.objects.select_related("session", "student").all()
    for key in ("status", "student", "session"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    if request.query_params.get("from"):
        qs = qs.filter(session__date__gte=request.query_params["from"])
    if request.query_params.get("to"):
        qs = qs.filter(session__date__lte=request.query_params["to"])
    if request.query_params.get("sport"):
        qs = qs.filter(session__sport_id=request.query_params["sport"])
    if request.query_params.get("batch"):
        qs = qs.filter(session__batch_id=request.query_params["batch"])
    if request.query_params.get("coach"):
        qs = qs.filter(session__coach_id=request.query_params["coach"])
    qs = scope_queryset(qs, request.user, "session__batch__school_id")
    qs = scope_to_parent(qs, request.user, "student_id")
    rows = [{"date": str(a.session.date), "session": a.session_id, "student": a.student.student_id,
             "status": a.status,
             "day_type": "Weekend" if a.session.date.weekday() >= 5 else "Weekday",
             "is_makeup": "Yes" if a.makeup_for_id else ""} for a in qs[:5000]]
    return _export(request, rows, "attendance_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def makeup_report(request):
    """Make-up attendance history: each make-up mark linked to the missed session.

    Both sides are real, separate attendance rows — the original absence keeps
    its own status (never auto-flipped to present) and the make-up is recorded
    against the session actually attended. Filters: from/to (make-up date),
    sport, batch, coach. School scoping applies."""
    from apps.attendance.models import AttendanceRecord
    qs = (AttendanceRecord.objects
          .filter(makeup_for__isnull=False)
          .select_related("session", "session__sport", "session__batch", "session__coach",
                          "student", "makeup_for"))
    if request.query_params.get("from"):
        qs = qs.filter(session__date__gte=request.query_params["from"])
    if request.query_params.get("to"):
        qs = qs.filter(session__date__lte=request.query_params["to"])
    if request.query_params.get("sport"):
        qs = qs.filter(session__sport_id=request.query_params["sport"])
    if request.query_params.get("batch"):
        qs = qs.filter(session__batch_id=request.query_params["batch"])
    if request.query_params.get("coach"):
        qs = qs.filter(session__coach_id=request.query_params["coach"])
    qs = scope_queryset(qs, request.user, "session__batch__school_id")
    qs = scope_to_parent(qs, request.user, "student_id")
    rows = []
    for a in qs[:5000]:
        orig = AttendanceRecord.objects.filter(session_id=a.makeup_for_id, student_id=a.student_id).first()
        rows.append({
            "student": a.student.student_id,
            "name": f"{a.student.first_name} {a.student.last_name}".strip(),
            "sport": a.session.sport.name if a.session.sport_id else "",
            "missed_date": str(a.makeup_for.date),
            "missed_session": a.makeup_for_id,
            "missed_status": orig.status if orig else "not_marked",
            "makeup_date": str(a.session.date),
            "makeup_session": a.session_id,
            "makeup_status": a.status,
            "coach": (f"{a.session.coach.first_name} {a.session.coach.last_name}".strip()
                      if a.session.coach_id else ""),
        })
    return _export(request, rows, "makeup_attendance_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def session_report(request):
    from apps.sessions.models import Session
    qs = Session.objects.select_related("batch", "coach", "sport").all()
    qs = scope_queryset(qs, request.user, "batch__school_id")
    for key in ("status", "sport", "batch", "coach"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    if request.query_params.get("from"):
        qs = qs.filter(date__gte=request.query_params["from"])
    if request.query_params.get("to"):
        qs = qs.filter(date__lte=request.query_params["to"])
    rows = [{"date": str(s.date), "batch": s.batch.name, "coach": f"{s.coach.first_name}",
             "sport": s.sport.name, "status": s.status} for s in qs[:2000]]
    return _export(request, rows, "session_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def trainer_fee_report(request):
    from apps.trainer_fees.models import TrainerPayable
    qs = TrainerPayable.objects.select_related("trainer", "session").all()
    for key in ("trainer", "status", "kind"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    qs = scope_queryset(qs, request.user, "trainer__school_id")
    rows = []
    for t in qs[:2000]:
        date = (str(t.session.date) if t.session_id
                else (str(t.period_start) if t.period_start else ""))
        rows.append({
            "trainer": f"{t.trainer.first_name} {t.trainer.last_name}".strip(),
            "trainer_id": t.trainer.trainer_id,
            "kind": t.kind,
            "session": t.session_id or "",
            "date": date,
            "rate_per_session": str(t.rule.rate_per_session) if (t.rule_id and t.kind == "session") else "",
            "period": (f"{t.period_start} to {t.period_end}" if t.kind != "session" and t.period_start else ""),
            "students": t.student_count if t.kind == "student_salary" else "",
            "amount": float(t.amount), "status": t.status,
        })
    return _export(request, rows, "trainer_fee_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def payment_history(request):
    from apps.payments.models import Payment
    qs = Payment.objects.select_related("student_fee").all().order_by("-created_at")
    qs = scope_queryset(qs, request.user, "student_fee__student__school_id")
    for key in ("status",):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    rows = [{"id": p.pk, "fee": p.student_fee_id, "amount": float(p.amount), "provider": p.provider,
             "status": p.status, "verified": p.verified, "receipt": p.receipt_no or ""} for p in qs[:2000]]
    return _export(request, rows, "payment_history")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def whatsapp_report(request):
    from apps.whatsapp.models import WhatsAppMessage
    qs = WhatsAppMessage.objects.all().order_by("-created_at")
    qs = scope_queryset(qs, request.user, "related_student__school_id")
    for key in ("status",):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    rows = [{"to": m.to_number, "status": m.status, "provider": m.provider,
             "created": m.created_at.isoformat()} for m in qs[:2000]]
    return _export(request, rows, "whatsapp_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def audit_report(request):
    from apps.audit.models import AuditLog
    qs = AuditLog.objects.select_related("user").all().order_by("-created_at")
    qs = scope_queryset(qs, request.user, "user__school_id")
    for key in ("module", "action"):
        v = request.query_params.get(key)
        if v:
            qs = qs.filter(**{key: v})
    rows = [{"at": a.created_at.isoformat(), "user": a.user.username if a.user else "",
             "action": a.action, "module": a.module, "record": a.record} for a in qs[:2000]]
    return _export(request, rows, "audit_report")


@api_view(["GET"])
@permission_classes([HasPerm("reports.view")])
def coach_allocation(request):
    from apps.batches.models import Batch
    qs = Batch.objects.select_related("coach", "sport").all()
    qs = scope_queryset(qs, request.user, "school_id")
    if request.query_params.get("coach"):
        qs = qs.filter(coach_id=request.query_params["coach"])
    if request.query_params.get("sport"):
        qs = qs.filter(sport_id=request.query_params["sport"])
    rows = [{"batch": b.name, "sport": b.sport.name, "coach": f"{b.coach.first_name}" if b.coach else "",
             "students": b.rosters.filter(is_active=True).count()} for b in qs[:1000]]
    return _export(request, rows, "coach_allocation")


@api_view(["GET"])
@permission_classes([HasPerm("schools.view")])
def platform(request):
    """Super Admin platform overview: every academy, its module switches and
    operational volume. Computed from the database, never hardcoded."""
    from apps.accounts.models import User
    from apps.academy.models import AcademyModule, Branch, School, Sport
    from apps.attendance.models import AttendanceRecord
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.fees.models import StudentFee
    from apps.parents.models import Parent
    from apps.sessions.models import Session
    from apps.students.models import Student

    from apps.common.modules import MODULE_CODES, module_label

    today = timezone.localdate()
    academies = School.objects.all().order_by("name")
    flags = {(f.school_id, f.module): f for f in AcademyModule.objects.all()}

    rows = []
    for sch in academies:
        disabled = [m for m in MODULE_CODES if flags.get((sch.pk, m)) is not None
                    and not flags[(sch.pk, m)].enabled]
        rows.append({
            "id": sch.pk,
            "name": sch.name,
            "code": sch.code,
            "is_active": sch.is_active,
            "branches": Branch.objects.filter(school=sch).count(),
            "locations": sch.locations.count(),
            "sports_offered": Batch.objects.filter(school=sch).values("sport").distinct().count(),
            "students": Student.objects.filter(school=sch).count(),
            "active_students": Student.objects.filter(school=sch, status="active").count(),
            "parents": Parent.objects.filter(student_links__student__school=sch).distinct().count(),
            "coaches": Coach.objects.filter(school=sch).count(),
            "users": User.objects.filter(school=sch).count(),
            "batches": Batch.objects.filter(school=sch).count(),
            "sessions_today": Session.objects.filter(date=today, batch__school=sch).count(),
            "attendance_today": AttendanceRecord.objects.filter(session__date=today,
                                                               session__batch__school=sch).count(),
            "fees_pending": StudentFee.objects.filter(student__school=sch)
                              .exclude(status__in=["paid", "cancelled", "refunded"])
                              .aggregate(t=Sum("amount"))["t"] or 0,
            "modules_total": len(MODULE_CODES),
            "modules_enabled": len(MODULE_CODES) - len(disabled),
            "modules_disabled": disabled,
        })

    role_counts = list(User.objects.values("role__name").annotate(count=Count("id")).order_by())
    return Response({
        "generated_at": timezone.now().isoformat(),
        "platform": {
            "academies": School.objects.count(),
            "active_academies": School.objects.filter(is_active=True).count(),
            "branches": Branch.objects.count(),
            "sports": Sport.objects.filter(is_active=True).count(),
            "students": Student.objects.count(),
            "parents": Parent.objects.count(),
            "coaches": Coach.objects.count(),
            "users": User.objects.count(),
            "sessions_today": Session.objects.filter(date=today).count(),
            "academy_module_switches": AcademyModule.objects.filter(enabled=False).count(),
        },
        "modules": [{"module": m, "label": module_label(m)} for m in MODULE_CODES],
        "roles": [{"role": r["role__name"] or "none", "count": r["count"]} for r in role_counts],
        "rows": rows,
    })
