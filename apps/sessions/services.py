"""Session scheduling rules: overlap conflict detection, working hours,
qualifications, configurable break buffers and available-slot suggestions.

Every check here is authoritative and reusable: the serializer calls them for
fast, friendly validation, and the viewset re-runs them inside a transaction
with row locks so concurrent requests can never double-book a trainer.
"""
from datetime import date as _date, datetime, time as _time, timedelta

from .models import Session

# Sessions in these states occupy the trainer's time and the academy slot.
# Cancelled sessions are intentionally excluded (their history is retained).
LIVE_STATUSES = ("scheduled", "in_progress")

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Fallback working window when a coach has no availability rows configured.
DEFAULT_DAY_START = _time(6, 0)
DEFAULT_DAY_END = _time(21, 0)


def get_break_minutes(school_id=None):
    """Configurable travel/preparation buffer between a trainer's sessions.

    Stored in the academy key-value settings store as `schedule.break_minutes`
    (default 0 = back-to-back sessions allowed). Resolved per academy: an
    academy override wins, otherwise the platform default applies."""
    try:
        from apps.academy.models import get_setting
        return max(0, int(get_setting("schedule.break_minutes", school_id, 0) or 0))
    except Exception:
        return 0


def parse_time(value):
    if value is None or isinstance(value, _time):
        return value
    text = str(value).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def parse_date(value):
    if value is None or isinstance(value, _date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _dt(day, t):
    return datetime.combine(day, t)


def overlaps(day, start, end, other_start, other_end, buffer_minutes=0):
    """True when [start, end] clashes with [other_start, other_end] allowing a
    minimum gap of ``buffer_minutes`` between them.

    With ``buffer_minutes == 0`` this reduces to the standard rule
    ``new_start < existing_end AND new_end > existing_start`` (back-to-back is
    allowed). A positive buffer also rejects sessions that sit too close
    together.
    """
    buf = timedelta(minutes=buffer_minutes or 0)
    return _dt(day, start) < _dt(day, other_end) + buf and _dt(day, other_start) < _dt(day, end) + buf


def live_sessions(coach, day, exclude_pk=None):
    if not coach or not day:
        return Session.objects.none()
    qs = Session.objects.filter(coach=coach, date=day, status__in=LIVE_STATUSES)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.select_related("sport", "batch")


def find_conflicts(coach, day, start, end, exclude_pk=None, buffer_minutes=None):
    """Return live sessions for ``coach`` on ``day`` that clash with the window."""
    if buffer_minutes is None:
        buffer_minutes = get_break_minutes(getattr(coach, "school_id", None))
    start = parse_time(start)
    end = parse_time(end)
    if not (coach and day and start and end):
        return []
    out = []
    for s in live_sessions(coach, day, exclude_pk=exclude_pk):
        if overlaps(day, start, end, s.start_time, s.end_time, buffer_minutes):
            out.append(s)
    return out


def _coach_name(coach):
    return f"{coach.first_name} {coach.last_name or ''}".strip()


def conflict_message(coach, conflict, day):
    """Friendly, specific conflict message naming sport, batch, date and time."""
    where = f"{conflict.sport.name} · {conflict.batch.name}" if conflict.sport_id and conflict.batch_id else (
        conflict.batch.name if conflict.batch_id else (conflict.sport.name if conflict.sport_id else "another session"))
    if day is None:
        day = conflict.date
    return (
        f"Coach {_coach_name(coach)} is already assigned to {where} from "
        f"{conflict.start_time.strftime('%I:%M %p')} to {conflict.end_time.strftime('%I:%M %p')} "
        f"on {day.isoformat()}. Please select another available coach or change the session timing."
    )


def week_off_conflict(coach, day):
    if coach and day and (coach.weekly_off_days or []) and day.weekday() in list(coach.weekly_off_days):
        return f"Coach {_coach_name(coach)} is on a weekly off ({WEEKDAY_NAMES[day.weekday()]}) on this date."
    return None


def qualification_conflict(coach, sport):
    """A coach may only be booked for sports they are qualified for.

    Coaches with no qualifications recorded are treated as unconstrained so
    legacy records keep working; once at least one sport is linked, the rule is
    enforced strictly."""
    if not coach or not sport:
        return None
    links = coach.sport_links.all()
    if not links.exists():
        return None
    if not links.filter(sport=sport).exists():
        return f"Coach {_coach_name(coach)} is not qualified for {sport.name}."
    return None


def availability_windows(coach, day):
    """Configured availability windows for the weekday (empty = unconstrained)."""
    from apps.coaches.models import CoachAvailability
    if not coach or not day:
        return []
    return list(CoachAvailability.objects.filter(
        coach=coach, weekday=day.weekday(), is_available=True).order_by("start_time"))


def availability_conflict(coach, day, start, end):
    """When a coach has windows for the weekday, the window must sit fully inside one."""
    start = parse_time(start)
    end = parse_time(end)
    windows = availability_windows(coach, day)
    if not windows:
        return None
    for w in windows:
        if w.start_time <= start and end <= w.end_time:
            return None
    spans = ", ".join(f"{w.start_time.strftime('%I:%M %p')}–{w.end_time.strftime('%I:%M %p')}" for w in windows)
    return (
        f"Coach {_coach_name(coach)} is only available {spans} on {WEEKDAY_NAMES[day.weekday()]}. "
        f"Pick a time inside those hours or another coach.")


def working_window(coach, day):
    """(start, end) for suggestions: the coach's first/last window, else defaults."""
    windows = availability_windows(coach, day)
    if windows:
        return windows[0].start_time, windows[-1].end_time
    return DEFAULT_DAY_START, DEFAULT_DAY_END


def suggest_slots(coach, day, duration_minutes, exclude_pk=None, limit=6, buffer_minutes=None):
    """Next free start times for ``coach`` on ``day`` within their working hours.

    Slots are aligned to the start of the working window and stepped by
    duration + break buffer. Returns a list of ``{start_time, end_time}`` dicts
    formatted as HH:MM, skipping any that overlap an existing live session.
    """
    day = parse_date(day)
    if not (coach and day and duration_minutes and duration_minutes > 0):
        return []
    if buffer_minutes is None:
        buffer_minutes = get_break_minutes(getattr(coach, "school_id", None))
    win_start, win_end = working_window(coach, day)
    step = timedelta(minutes=int(duration_minutes) + int(buffer_minutes))
    dur = timedelta(minutes=int(duration_minutes))
    cursor = _dt(day, win_start)
    hard_stop = _dt(day, win_end)
    conflicts = [(s.start_time, s.end_time) for s in live_sessions(coach, day, exclude_pk=exclude_pk)]
    out = []
    while cursor + dur <= hard_stop and len(out) < limit:
        st = cursor.time()
        en = (cursor + dur).time()
        clash = any(overlaps(day, st, en, cs, ce, buffer_minutes) for cs, ce in conflicts)
        if not clash:
            out.append({"start_time": st.strftime("%H:%M"), "end_time": en.strftime("%H:%M")})
        cursor += step
    return out


def day_conflict_ids(sessions):
    """Given sessions (already filtered to one day), return the set of pks that
    overlap another live session for the same coach (legacy/dirty data guard)."""
    by_coach = {}
    for s in sessions:
        if s.status in LIVE_STATUSES and s.coach_id:
            by_coach.setdefault(s.coach_id, []).append(s)
    flagged = set()
    for group in by_coach.values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if overlaps(a.date, a.start_time, a.end_time, b.start_time, b.end_time, 0):
                    flagged.add(a.pk)
                    flagged.add(b.pk)
    return flagged


def ensure_session_roster(session):
    """Backfill a session's roster from its (active) batch roster.

    A session snapshots its roster at creation; students enrolled into the
    batch afterwards were never linked, so a session could be left with an
    empty/stale roster and attendance (including AI photo analysis) had
    nothing to work against. This adds the missing active batch students to
    the session. It is idempotent and only ever ADDS rows — an attendance
    record already written is never removed. Returns the number added.
    """
    from apps.batches.models import BatchStudent
    from .models import SessionStudent

    if session is None or session.batch_id is None:
        return 0
    active_batch_ids = set(BatchStudent.objects.filter(
        batch_id=session.batch_id, is_active=True).values_list("student_id", flat=True))
    if not active_batch_ids:
        return 0
    linked = set(SessionStudent.objects.filter(
        session_id=session.pk).values_list("student_id", flat=True))
    missing = active_batch_ids - linked
    if not missing:
        return 0
    SessionStudent.objects.bulk_create(
        [SessionStudent(session_id=session.pk, student_id=sid) for sid in missing],
        ignore_conflicts=True,
    )
    return len(missing)

