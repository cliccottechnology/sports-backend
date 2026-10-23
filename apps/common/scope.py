"""School (multi-tenancy) scoping.

- Superusers, super_admin role holders, and legacy staff without a school
  see everything (backward compatible).
- Users with `school` set (onboarded school admins) see only their school's
  rows. Every scoped queryset below is the security boundary — the frontend
  only mirrors it.
"""


def user_school_id(user):
    if not user or not user.is_authenticated:
        return None
    if getattr(user, "is_superuser", False):
        return None
    if getattr(getattr(user, "role", None), "name", None) == "super_admin":
        return None
    return getattr(user, "school_id", None)


def scope_queryset(qs, user, *paths):
    """Filter `qs` to the user's school following any of the given FK paths
    (e.g. "school_id", "batch__school_id"). Unrestricted users pass through."""
    sid = user_school_id(user)
    if sid is None:
        return qs
    from django.db.models import Q
    q = Q()
    for path in paths:
        q |= Q(**{path: sid})
    return qs.filter(q)


def parent_student_ids(user):
    """Student pks visible to a parent-role account via its linked parent.

    Returns None when the user is not a parent (no parent restriction).
    Parent-role accounts see ONLY linked students — everywhere this is
    applied, which is the backend boundary for the parent portal.
    """
    if not user or not user.is_authenticated or getattr(user, "is_superuser", False):
        return None
    if getattr(getattr(user, "role", None), "name", None) != "parent":
        return None
    parent_id = getattr(user, "parent_profile_id", None)
    if not parent_id:
        return []
    from apps.parents.models import StudentParent
    return list(StudentParent.objects.filter(parent_id=parent_id).values_list("student_id", flat=True))


def scope_to_parent(qs, user, student_path):
    """Restrict a queryset to a parent's linked students (no-op for others)."""
    ids = parent_student_ids(user)
    if ids is None:
        return qs
    return qs.filter(**{f"{student_path}__in": ids})


def _pk(value):
    if value in (None, ""):
        return None
    pk = getattr(value, "pk", value)
    try:
        return int(pk)
    except (TypeError, ValueError):
        return "invalid"


def ensure_school(request, obj_school, field_name="school"):
    """For creates: stamp the user's school when absent, reject mismatches.
    Accepts a pk or a model instance; returns a pk (or None for unrestricted)."""
    from rest_framework.exceptions import PermissionDenied, ValidationError
    user = request.user
    sid = user_school_id(user)
    if sid is None:
        return _pk(obj_school)
    want = _pk(obj_school)
    if want == "invalid":
        raise ValidationError({field_name: "Invalid school."})
    if want is None:
        return sid
    if want != int(sid):
        raise PermissionDenied("Outside your school's scope.")
    return want
