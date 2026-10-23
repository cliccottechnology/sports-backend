"""Per-academy module enablement, enforced on every API request.

The Super Admin switches a module off for one academy and that academy's users
lose the feature immediately — on the server, not just in the sidebar. The
frontend additionally hides disabled modules, but this module is the boundary.

Backward compatible: a missing AcademyModule row means "enabled", so existing
academies keep every feature until an administrator turns one off. Super admins
and school-less staff are unrestricted (they administer the platform itself).
"""
from apps.accounts.rbac_catalog import MODULE_ORDER

MODULE_CODES = tuple(MODULE_ORDER)


def module_label(code: str) -> str:
    return code.replace("_", " ").title()


def module_enabled(user, module):
    """True unless this module has been switched off for the user's academy."""
    if not module:
        return True
    from apps.common.scope import user_school_id
    sid = user_school_id(user)
    if sid is None:
        return True
    from apps.academy.models import AcademyModule
    flag = AcademyModule.objects.filter(school_id=sid, module=module).first()
    return True if flag is None else bool(flag.enabled)


def module_config(user, module, default=None):
    """Effective config payload for a module (academy override only)."""
    from apps.common.scope import user_school_id
    sid = user_school_id(user)
    if sid is None:
        return default
    from apps.academy.models import AcademyModule
    flag = AcademyModule.objects.filter(school_id=sid, module=module).first()
    return default if flag is None else (flag.config or default)


def disabled_modules(school_id):
    """Module codes switched off for an academy (for dashboards / nav shaping)."""
    if school_id is None:
        return []
    from apps.academy.models import AcademyModule
    return list(AcademyModule.objects.filter(school_id=school_id, enabled=False)
                .values_list("module", flat=True))


class ModuleEnabled:
    """DRF permission: denies the request when `view.module` is off for the
    caller's academy. Viewsets opt in by declaring `module = "<code>"`."""

    def __call__(self):
        return self

    def has_permission(self, request, view):
        module = getattr(view, "module", None) or getattr(view, "perm_module", None)
        if not module:
            return True
        user = request.user
        if not user or not user.is_authenticated:
            return False
        from apps.common.scope import user_school_id
        if user_school_id(user) is None:
            return True
        return module_enabled(user, module)
