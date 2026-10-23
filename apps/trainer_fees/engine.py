"""Trainer fee engine: only completed/validated sessions are eligible by default."""
from datetime import date, timedelta

from .models import TrainerFeeRule


def find_rule(trainer_id, *, sport_id=None, batch_id=None, session_type_id=None, on_date=None):
    qs = TrainerFeeRule.objects.filter(trainer_id=trainer_id, is_active=True)
    if on_date:
        qs = qs.filter(effective_from__lte=on_date).filter(
            models_Q_effective_to(on_date))
    # Priority: batch+type > batch > sport+type > sport > generic trainer rule
    candidates = list(qs.select_related("sport", "batch", "session_type"))
    def score(r):
        return ((1 if r.batch_id == batch_id and batch_id else 0) * 8
                + (1 if r.session_type_id == session_type_id and session_type_id else 0) * 4
                + (1 if r.sport_id == sport_id and sport_id else 0) * 2
                + (1 if not r.batch_id and not r.sport_id and not r.session_type_id else 0))
    # filter to rules that don't contradict the session
    valid = [r for r in candidates
             if (not r.batch_id or r.batch_id == batch_id)
             and (not r.sport_id or r.sport_id == sport_id)
             and (not r.session_type_id or r.session_type_id == session_type_id)]
    if not valid:
        return None
    return sorted(valid, key=score, reverse=True)[0]


def models_Q_effective_to(on_date):
    from django.db import models
    return models.Q(effective_to__isnull=True) | models.Q(effective_to__gte=on_date)


def require_session_validation(school_id=None):
    """Academy policy flag: when on, EVERY session must be explicitly validated
    before it can credit trainer salary (Settings → trainer.require_session_validation).
    Default off — completed sessions remain eligible, preserving legacy data."""
    try:
        from apps.academy.models import get_setting
        return bool(get_setting("trainer.require_session_validation", school_id, False))
    except Exception:
        return False


def session_is_eligible(session):
    """Salary eligibility gate for one session.

    Rules (in order):
    - Only completed sessions qualify.
    - Make-up sessions never credit automatically: they stay out until an
      authorized admin validates them (validated_for_trainer_fee).
    - When the academy requires validation for all sessions, every session must
      carry validated_for_trainer_fee.
    Cancelled / unvalidated sessions are always excluded.
    """
    if session.status != "completed":
        return False
    school_id = getattr(getattr(session, "batch", None), "school_id", None)
    is_makeup = getattr(session, "is_makeup", False) or getattr(session, "makeup_for_id", None)
    if is_makeup and not session.validated_for_trainer_fee:
        return False
    if require_session_validation(school_id) and not session.validated_for_trainer_fee:
        return False
    return True


def compute_payables_for_session(session):
    """Create the TrainerPayable for a completed session. Returns payable or None.

    History-safe: the amount is snapshotted once at creation and NEVER
    rewritten by later runs — a rate change only affects sessions completed
    afterwards. Re-running generate is idempotent (one payable per session).
    """
    from .models import TrainerPayable
    if not session_is_eligible(session):
        return None
    rule = find_rule(session.coach_id, sport_id=session.sport_id, batch_id=session.batch_id,
                     session_type_id=session.session_type_id, on_date=session.date)
    if not rule or rule.payment_model != "per_session":
        return None
    payable, _ = TrainerPayable.objects.get_or_create(
        session=session,
        defaults={"trainer_id": session.coach_id, "rule": rule, "amount": rule.rate_per_session},
    )
    return payable


def find_salary_rule(trainer_id, kind, on_date):
    """Active weekly/monthly rule effective on a date (latest wins)."""
    from .models import TrainerFeeRule
    return (TrainerFeeRule.objects
            .filter(trainer_id=trainer_id, payment_model=kind, is_active=True,
                    effective_from__lte=on_date)
            .filter(models_Q_effective_to(on_date))
            .order_by("-effective_from").first())


def week_starts(from_date, to_date):
    """Monday starts of every calendar week overlapping [from_date, to_date]."""
    cur = from_date - timedelta(days=from_date.weekday())
    out = []
    while cur <= to_date:
        out.append(cur)
        cur += timedelta(days=7)
    return out


def month_starts(from_date, to_date):
    """First-of-month starts overlapping [from_date, to_date]."""
    out = []
    cur = from_date.replace(day=1)
    while cur <= to_date:
        out.append(cur)
        cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def compute_salary_for_period(trainer_id, kind, period_start, period_end):
    """Create the fixed salary payable for one trainer + period. Idempotent."""
    return _salary_get_or_none(trainer_id, kind, period_start, period_end)[0]


def generate_salaries(trainer_ids, from_date, to_date):
    """Weekly/monthly salary payables for trainers with fixed-salary rules."""
    from .models import TrainerFeeRule
    created = 0
    rules = (TrainerFeeRule.objects
             .filter(trainer_id__in=list(trainer_ids), is_active=True,
                     payment_model__in=["weekly", "monthly"])
             .values_list("trainer_id", "payment_model").distinct())
    for trainer_id, kind in rules:  # kind: "weekly" | "monthly"
        starts = week_starts(from_date, to_date) if kind == "weekly" else month_starts(from_date, to_date)
        for start in starts:
            if kind == "weekly":
                end = min(start + timedelta(days=6), to_date)
            else:
                month_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                end = min(month_end, to_date)
            if start > to_date:
                continue
            _, was_created = _salary_get_or_none(trainer_id, kind, start, end)
            if was_created:
                created += 1
    return created


def _salary_get_or_none(trainer_id, kind, start, end):
    from .models import TrainerPayable
    rule = find_salary_rule(trainer_id, kind, start)
    if not rule or rule.fixed_amount <= 0:
        return None, False
    return TrainerPayable.objects.get_or_create(
        trainer_id=trainer_id, kind=f"{kind}_salary", period_start=start,
        defaults={"period_end": end, "rule": rule, "amount": rule.fixed_amount},
    )


# --------------------------------------------------------------------------
# Method selection: exactly one active salary method per trainer per period.
# --------------------------------------------------------------------------

def active_config(trainer_id, on_date):
    """The single active TrainerSalaryConfig governing a trainer on a date.

    Method changes create non-overlapping configs, so at most one matches.
    Latest effective_from wins if legacy data ever overlaps."""
    from .models import TrainerSalaryConfig
    return (TrainerSalaryConfig.objects
            .filter(trainer_id=trainer_id, is_active=True, effective_from__lte=on_date)
            .filter(models_Q_effective_to(on_date))
            .order_by("-effective_from").first())


def find_student_config(trainer_id, on_date):
    cfg = active_config(trainer_id, on_date)
    return cfg if cfg and cfg.method == "student_based" else None


# --------------------------------------------------------------------------
# Student-based salary: snapshot of eligible students x rate, or count bands.
# --------------------------------------------------------------------------

def eligible_students(trainer_id, config, from_date, to_date):
    """Students a trainer is paid for in [from_date, to_date].

    Eligible = active student, enrolled on/before period end, rostered (active
    link) to a batch the trainer coaches — either directly (Batch.coach) or via
    a CoachAssignment overlapping the period. `config.sport` narrows to one
    sport. Counting is distinct, so a student in several batches counts once.
    """
    from django.db.models import Q
    from apps.batches.models import BatchStudent
    from apps.coaches.models import CoachAssignment
    from apps.students.models import Student

    student_ids = set(
        BatchStudent.objects.filter(is_active=True, batch__coach_id=trainer_id)
        .values_list("student_id", flat=True))
    assigned_batches = (CoachAssignment.objects
                        .filter(coach_id=trainer_id, assigned_from__lte=to_date)
                        .filter(Q(assigned_to__isnull=True) | Q(assigned_to__gte=from_date))
                        .values_list("batch_id", flat=True))
    student_ids |= set(
        BatchStudent.objects.filter(is_active=True, batch_id__in=assigned_batches)
        .values_list("student_id", flat=True))

    qs = Student.objects.filter(pk__in=student_ids, status="active",
                                enrollment_date__lte=to_date)
    if config and config.sport_id:
        qs = qs.filter(sport_id=config.sport_id)
    return qs.distinct()


def count_eligible_students(trainer_id, config, from_date, to_date):
    return eligible_students(trainer_id, config, from_date, to_date).count()


def student_salary_amount(config, count):
    """Amount for a student count, or None when the config cannot price it.

    Banded mode matches the [min, max] band. Counts above the highest finite
    band follow `above_top_band`: cap at the top band, charge the per-student
    rate, or reject (None)."""
    if count is None or count <= 0:
        return None
    if config.student_mode == "per_student":
        return config.student_rate * count if config.student_rate else None
    bands = list(config.bands.all())  # ordered by min_students
    for band in bands:
        if band.covers(count):
            return band.amount
    if bands:
        top = bands[-1]
        if count > (top.max_students or 0):
            if config.above_top_band == "cap":
                return top.amount
            if config.above_top_band == "per_student" and config.student_rate:
                return config.student_rate * count
        return None  # above_top_band == "reject", or no priceable rule
    return None


def compute_student_salary(config, period_start, period_end):
    """Create the student-based payable for one trainer + period. Idempotent.

    The eligible count and amount are snapshotted at creation; later roster or
    rate changes never rewrite an existing payable."""
    from .models import TrainerPayable
    count = count_eligible_students(config.trainer_id, config, period_start, period_end)
    amount = student_salary_amount(config, count)
    if not amount or amount <= 0:
        return None
    payable, _ = TrainerPayable.objects.get_or_create(
        trainer_id=config.trainer_id, kind="student_salary", period_start=period_start,
        defaults={"period_end": period_end, "config": config,
                  "student_count": count, "amount": amount},
    )
    return payable


def generate_student_salaries(trainer_ids, from_date, to_date):
    """Monthly student-based salary payables for trainers on that method."""
    from .models import TrainerSalaryConfig
    created = 0
    trainer_ids = set(trainer_ids)
    configured = set(TrainerSalaryConfig.objects
                     .filter(trainer_id__in=list(trainer_ids), is_active=True,
                             method="student_based")
                     .values_list("trainer_id", flat=True))
    for trainer_id in configured:
        for start in month_starts(from_date, to_date):
            if start > to_date:
                continue
            config = find_student_config(trainer_id, start)
            if not config:
                continue
            month_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            end = min(month_end, to_date)
            if compute_student_salary(config, start, end):
                created += 1
    return created
