from rest_framework.permissions import BasePermission


def _implies(held: str, required: str) -> bool:
    """Legacy coarse codes imply their granular set.

    `<module>.manage` grants every `<module>.<action>`, and the retired
    coarse codes (attendance.mark, masters.view, …) imply their documented
    granular equivalents. Evaluated at request time so it works for any role,
    including ones created after the rollout migration.
    """
    if held == required:
        return True
    if "." in required:
        module = required.split(".", 1)[0]
        if held == f"{module}.manage":
            return True
    try:
        from apps.accounts.rbac_catalog import LEGACY_MAP
    except Exception:
        return False
    return required in LEGACY_MAP.get(held, [])


class HasPerm(BasePermission):
    """Backend-enforced RBAC. `required` is a permission codename like 'fees.manage'.

    Checks current database state on every request, so grants and revocations
    apply immediately — no re-login needed, and revoked tokens lose power at
    most when the short-lived access token expires (deactivation kills even
    that via ActiveJWTAuthentication).
    """

    def __init__(self, required=None):
        self.required = required

    def __call__(self):
        # DRF's `permission_classes = [...]` and `@permission_classes([...])`
        # expect *classes* and instantiate them via `perm()`. We are often
        # used as `HasPerm("x")` (already an instance), so allow calling
        # the instance to return itself.
        return self

    def has_permission(self, request, view):
        required = getattr(view, "required_permission", self.required)
        if not required:
            return bool(request.user and request.user.is_authenticated)
        user = request.user
        if not user or not user.is_authenticated:
            return False
        module = getattr(view, "module", None) or getattr(view, "perm_module", None)
        if module:
            from apps.common.modules import module_enabled
            if not module_enabled(user, module):
                return False
        if user.is_superuser:
            return True
        held = set(user.role.permissions.values_list("code", flat=True)) if getattr(user, "role", None) else set()
        return any(_implies(h, required) for h in held)


def require(required):
    def deco(cls):
        cls.required_permission = required
        return cls
    return deco


class HasAnyPerm(BasePermission):
    """Passes when the user holds ANY of the given codes (with manage fallback)."""

    def __init__(self, *codes):
        self.codes = codes

    def __call__(self):
        return self

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        held = set(user.role.permissions.values_list("code", flat=True)) if getattr(user, "role", None) else set()
        return any(_implies(h, c) for h in held for c in self.codes)


class IsParent(BasePermission):
    """Parent-only endpoints. The assigned role is read from the server-side
    user record on every request — a role or parent id supplied by the client
    (in the body, query or token payload) can never widen access. Trainer,
    admin and super-admin accounts are rejected unless the policy explicitly
    lists them, keeping parent-only APIs closed to teacher/admin tokens.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(getattr(user, "role", None), "name", None) == "parent"
        )


class GranularPermissions:
    """Drop-in `get_permissions` for standard CRUD viewsets.

    list/retrieve → `<module>.view`, create → `.create`, update → `.edit`,
    destroy → `.archive`. Override with `extra_action_perms = {"action": "code"}`
    (None = public) or the `*_perm` attributes for special cases.
    """

    perm_module = ""
    list_perm = None
    retrieve_perm = None
    create_perm = None
    edit_perm = None
    destroy_perm = None
    extra_action_perms = {}

    def get_permissions(self):
        from rest_framework.permissions import AllowAny
        action = getattr(self, "action", None)
        extras = getattr(self, "extra_action_perms", None) or {}
        if action in extras:
            code = extras[action]
            return [AllowAny()] if code is None else [HasPerm(code)]
        m = self.perm_module
        if action == "list":
            code = self.list_perm or f"{m}.view"
        elif action == "retrieve":
            code = self.retrieve_perm or f"{m}.view"
        elif action == "create":
            code = self.create_perm or f"{m}.create"
        elif action in ("update", "partial_update"):
            code = self.edit_perm or f"{m}.edit"
        elif action == "destroy":
            code = self.destroy_perm or f"{m}.archive"
        else:
            code = f"{m}.manage"
        return [HasPerm(code)]
