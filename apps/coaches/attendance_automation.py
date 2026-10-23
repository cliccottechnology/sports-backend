"""Automatic trainer-absence marking and missed-attendance notifications.

Runs from a scheduled job (management command or Celery beat) — never from the
mobile app. Every step is idempotent: the unique (session, coach) constraint on
TrainerCheckIn plus existence checks mean the same absence is never recorded
twice, no matter how often the sweep runs.

Respects:
- cancelled sessions (skipped),
- reschedules/substitutions (the session's *current* date/coach is used),
- approved leave (TrainerLeave), and
- existing check-ins (any status) — a human mark is never overwritten.

Notification failures are swallowed so they can never block attendance
processing.
"""
from datetime import datetime, timedelta

from django.utils import timezone

DEFAULT_SETTINGS = {
    "trainer.auto_absent_enabled": True,
    "trainer.attendance_grace_minutes": 15,
    "trainer.auto_absent_notify_trainer": True,
    "trainer.auto_absent_notify_admins": True,
    "trainer.auto_absent_notify_roles": [],
    "trainer.correction_deadline_days": 7,
    "trainer.reminder_minutes_before": 0,
    "trainer.approval_roles": [],
}

CONFIG_META = [
    ("trainer.auto_absent_enabled", "Enabled", "bool",
     "Automatically mark a trainer absent when the attendance deadline passes with no check-in."),
    ("trainer.attendance_grace_minutes", "Grace period after session end (minutes)", "int",
     "Minutes after the session ends before an unmarked trainer is auto-marked absent."),
    ("trainer.reminder_minutes_before", "Reminder before deadline (minutes, 0 = off)", "int",
     "Send the trainer an in-app reminder this many minutes before the deadline."),
    ("trainer.auto_absent_notify_trainer", "Notify the trainer", "bool",
     "Send the trainer an in-app notification explaining the missed attendance."),
    ("trainer.auto_absent_notify_admins", "Notify admins / Super Admin", "bool",
     "Notify admins and Super Admins that a trainer did not mark attendance."),
    ("trainer.auto_absent_notify_roles", "Extra notify roles (role names)", "list",
     "Additional role names to notify about missed trainer attendance."),
    ("trainer.correction_deadline_days", "Correction request window (days)", "int",
     "How long after a session a trainer may submit an attendance correction request."),
    ("trainer.approval_roles", "Additional approval roles (role names)", "list",
     "Extra role names allowed to approve correction requests (besides attendance.correct)."),
]

# How far back the sweep looks for unmarked sessions (bounds the scan).
LOOKBACK_DAYS = 45


def config_for(school_id):
    """Effective automation config for an academy (code defaults when unset)."""
    from apps.academy.models import get_setting
    cfg = {}
    for key, default in DEFAULT_SETTINGS.items():
        value = get_setting(key, school_id, default)
        cfg[key] = default if value is None else value
    return cfg


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def local_midnight_buffer():
    return timezone.localtime(timezone.now()).date()


def attendance_deadline(session, grace_minutes):
    """Aware datetime of the deadline: session end + grace, in the academy TZ."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Kolkata")
    end = datetime.combine(session.date, session.end_time)
    return timezone.make_aware(end, tz) + timedelta(minutes=max(0, _as_int(grace_minutes, 15)))


def _describe(session):
    coach = session.coach
    name = f"{coach.first_name} {coach.last_name or ''}".strip() if coach else "Trainer"
    sport = getattr(session.sport, "name", "") if session.sport_id else ""
    batch = getattr(session.batch, "name", "") if session.batch_id else ""
    return name, sport, batch, session.date, session.start_time, session.end_time


def _missed_message(session, deadline):
    name, sport, batch, date, start, end = _describe(session)
    time_range = f"{start.strftime('%I:%M %p').lstrip('0')}–{end.strftime('%I:%M %p').lstrip('0')}"
    return (
        f"Your attendance for the {time_range} {sport} session"
        + (f" ({batch})" if batch else "")
        + f" on {date} was automatically marked Absent because attendance was not recorded "
        f"before the deadline ({timezone.localtime(deadline).strftime('%I:%M %p').lstrip('0')}). "
        "Please contact Admin or submit an attendance correction request if you attended the session."
    )


def _notify_trainer(session, deadline, cfg):
    if not cfg.get("trainer.auto_absent_notify_trainer", True):
        return 0
    from apps.notifications.utils import notify, users_for_coach
    message = _missed_message(session, deadline)
    sent = 0
    for user in users_for_coach(session.coach):
        if notify(user, "Attendance missed — marked Absent", message):
            sent += 1
    return sent


def _notify_admins(session, deadline, cfg):
    if not cfg.get("trainer.auto_absent_notify_admins", True):
        return 0
    from apps.notifications.utils import admin_recipients, notify
    from apps.notifications.utils import users_for_coach
    name, sport, batch, date, start, end = _describe(session)
    school_id = getattr(session.batch, "school_id", None) if session.batch_id else None
    message = (
        f"Trainer {name} did not mark attendance for the "
        f"{start.strftime('%I:%M %p').lstrip('0')}–{end.strftime('%I:%M %p').lstrip('0')} "
        f"{sport} session" + (f" ({batch})" if batch else "")
        + f" on {date}. It was automatically recorded as Absent."
    )
    extra_roles = cfg.get("trainer.auto_absent_notify_roles") or []
    recipients = admin_recipients(school_id, extra_role_names=extra_roles)
    coach_user_ids = {u.pk for u in users_for_coach(session.coach)}
    sent = 0
    for user in recipients:
        if user.pk in coach_user_ids:  # don't double-notify a trainer-admin
            continue
        if notify(user, "Trainer missed attendance", message):
            sent += 1
    return sent


def _send_reminder(session, cfg):
    """Best-effort reminder before the deadline; idempotent per (coach, session)."""
    from .models import TrainerAttendanceReminder
    from apps.notifications.utils import notify, users_for_coach
    try:
        _, created = TrainerAttendanceReminder.objects.get_or_create(
            coach_id=session.coach_id, session_id=session.pk)
    except Exception:
        return 0
    if not created:
        return 0
    name, sport, batch, date, start, end = _describe(session)
    message = (
        f"Reminder: mark your attendance for the "
        f"{start.strftime('%I:%M %p').lstrip('0')}–{end.strftime('%I:%M %p').lstrip('0')} "
        f"{sport} session" + (f" ({batch})" if batch else "")
        + f" on {date} before the deadline."
    )
    sent = 0
    for user in users_for_coach(session.coach):
        if notify(user, "Mark your attendance", message, dedupe_seconds=0):
            sent += 1
    return sent


def process_trainer_attendance(now=None, school_id=None, dry_run=False):
    """Run one sweep. Returns a summary dict. Safe to run repeatedly."""
    from apps.sessions.models import Session
    from .models import TrainerCheckIn, TrainerLeave

    now = now or timezone.now()
    today = timezone.localtime(now).date()
    since = today - timedelta(days=LOOKBACK_DAYS)

    qs = (Session.objects
          .filter(coach__isnull=False, date__gte=since, date__lte=today)
          .exclude(status="cancelled")
          .select_related("coach", "sport", "batch"))
    if school_id is not None:
        qs = qs.filter(batch__school_id=school_id)

    result = {"considered": 0, "auto_absent": 0, "reminders": 0,
              "skipped_existing": 0, "skipped_leave": 0, "skipped_not_due": 0,
              "notifications": 0, "dry_run": bool(dry_run)}

    for session in qs:
        sid = getattr(session.batch, "school_id", None) if session.batch_id else None
        cfg = config_for(sid)
        if not cfg.get("trainer.auto_absent_enabled", True):
            continue
        result["considered"] += 1
        grace = cfg.get("trainer.attendance_grace_minutes", 15)
        deadline = attendance_deadline(session, grace)

        existing = TrainerCheckIn.objects.filter(session_id=session.pk, coach_id=session.coach_id).exists()
        on_leave = TrainerLeave.covers(session.coach_id, session)

        # Optional reminder window (deadline - N minutes .. deadline).
        reminder_before = _as_int(cfg.get("trainer.reminder_minutes_before"), 0)
        if (reminder_before > 0 and not existing and not on_leave
                and deadline - timedelta(minutes=reminder_before) <= now < deadline):
            if dry_run:
                result["reminders"] += 1
            else:
                result["reminders"] += _send_reminder(session, cfg)

        if now < deadline:
            result["skipped_not_due"] += 1
            continue
        if existing:
            result["skipped_existing"] += 1
            continue
        if on_leave:
            result["skipped_leave"] += 1
            continue

        if dry_run:
            result["auto_absent"] += 1
            continue

        try:
            checkin, created = TrainerCheckIn.objects.get_or_create(
                session_id=session.pk, coach_id=session.coach_id,
                defaults={"status": "absent", "auto_marked": True,
                          "notes": "Automatically marked absent (no attendance before deadline)."})
        except Exception:
            created = False
        if not created:
            result["skipped_existing"] += 1
            continue
        result["auto_absent"] += 1
        # Notifications must never break processing.
        try:
            result["notifications"] += _notify_trainer(session, deadline, cfg)
            result["notifications"] += _notify_admins(session, deadline, cfg)
        except Exception:
            pass
        try:
            from apps.audit.utils import log_audit
            log_audit(None, "trainer.auto_absent", "coaches", f"session:{session.pk}",
                      new={"coach": session.coach_id, "status": "absent",
                           "deadline": deadline.isoformat()})
        except Exception:
            pass

    return result
