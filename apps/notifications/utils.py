"""Small in-app notification helpers shared across apps.

Creation is best-effort: a notification failure must never break the caller
(e.g. attendance processing). Push is attempted only when a real provider is
configured, mirroring the customization app's helper.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

DEDUPE_WINDOW_SECONDS = 60


def notify(user, title, message, dedupe_seconds=DEDUPE_WINDOW_SECONDS):
    """Create one in-app notification, skipping an exact duplicate fired within
    a short window. Returns the Notification or None. Never raises."""
    try:
        if not user or not getattr(user, "is_active", False):
            return None
        from .models import Notification
        if dedupe_seconds:
            recent = timezone.now() - timedelta(seconds=dedupe_seconds)
            if Notification.objects.filter(
                    user=user, title=title, message=message, channel="in_app",
                    created_at__gte=recent).exists():
                return None
        obj = Notification.objects.create(user=user, title=title, message=message, channel="in_app")
        try:
            from .push import get_push_provider
            provider = get_push_provider()
            if provider.name != "mock" and provider.is_configured():
                provider.send(title=title, body=message, user_id=user.pk)
        except Exception:
            pass
        return obj
    except Exception:
        return None


def notify_users(users, title, message):
    for u in users:
        notify(u, title, message)


def super_admin_users():
    from apps.accounts.models import User
    return list(User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__name="super_admin")).distinct())


def admin_recipients(school_id=None, extra_role_names=(), perm_codes=("coaches.manage", "attendance.correct")):
    """Active admins who should hear about a trainer issue: super admins, plus
    school-scoped users holding any of `perm_codes` or any `extra_role_names`."""
    from apps.accounts.models import User
    ids = set(u.pk for u in super_admin_users())
    qs = User.objects.filter(is_active=True).exclude(role__isnull=True).select_related("role")
    if school_id is not None:
        qs = qs.filter(Q(school_id=school_id) | Q(school__isnull=True))
    extra = set(extra_role_names or ())
    for u in qs:
        if extra and getattr(getattr(u, "role", None), "name", None) in extra:
            ids.add(u.pk)
            continue
        for code in perm_codes:
            if u.has_perm_code(code):
                ids.add(u.pk)
                break
    return list(User.objects.filter(pk__in=ids, is_active=True))


def users_for_coach(coach):
    """Login accounts linked to a coach profile."""
    from apps.accounts.models import User
    return list(User.objects.filter(is_active=True, coach_profile_id=getattr(coach, "pk", None)))
