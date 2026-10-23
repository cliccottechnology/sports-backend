from django.contrib.auth import get_user_model
from django.db.models import Q
from rest_framework import generics, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from apps.accounts.auth import (
    ParentLoginSerializer,
    SessionTokenObtainPairSerializer,
    SessionTokenRefreshSerializer,
    TrainerLoginSerializer,
)
from apps.audit.utils import log_audit
from apps.common.permissions import HasPerm
from .models import Permission, Role
from .serializers import PermissionSerializer, RoleSerializer, UserCreateSerializer, UserSerializer

User = get_user_model()


class SessionLoginView(TokenObtainPairView):
    """Base for every login portal. Decorates a successful response with the
    authenticated user's identity and records the audit entry. Role-scoped
    subclasses only swap ``serializer_class`` (which enforces the role); the
    credential check is unchanged for all portals.
    """

    permission_classes = [AllowAny]

    @staticmethod
    def _resolve_user(request):
        identifier = request.data.get("username") or request.data.get("phone") or ""
        if not identifier:
            return None
        return (
            User.objects.select_related("role")
            .filter(Q(username__iexact=identifier) | Q(email__iexact=identifier) | Q(phone=identifier))
            .first()
        )

    def post(self, request, *args, **kwargs):
        resp = super().post(request, *args, **kwargs)
        if resp.status_code == 200:
            user = self._resolve_user(request)
            if user is not None:
                from django.utils import timezone
                user.last_login = timezone.now()
                user.save(update_fields=["last_login"])
                resp.data["user"] = UserSerializer(user).data
                portal = getattr(self.get_serializer_class(), "portal", None)
                if portal:
                    resp.data["portal"] = portal
                log_audit(request, "login", "auth", f"user:{user.pk}")
        return resp


class LoginView(SessionLoginView):
    """Shared academy login: admins, super admins and (legacy) trainers/parents.

    Kept unchanged for backward compatibility with the existing apps.
    """

    serializer_class = SessionTokenObtainPairSerializer


class ParentLoginView(SessionLoginView):
    """Parent App login: phone (or username/email) + password. Only accounts
    whose assigned role is ``parent`` may authenticate here — enforced on the
    server, never trusting a portal/role sent by the client."""

    serializer_class = ParentLoginSerializer


class TrainerLoginView(SessionLoginView):
    """Trainer App login: restricted to accounts whose role is ``coach``."""

    serializer_class = TrainerLoginSerializer


class SessionTokenRefreshView(TokenRefreshView):
    """Refresh bound by the absolute session deadline (see auth.py)."""
    serializer_class = SessionTokenRefreshSerializer


class LogoutView(generics.GenericAPIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        # Revoke the refresh token when the client hands it over; the client
        # always discards its own tokens regardless.
        refresh = request.data.get("refresh", "")
        if refresh:
            try:
                from rest_framework_simplejwt.tokens import RefreshToken
                RefreshToken(refresh).blacklist()
            except Exception:
                pass
        log_audit(request, "logout", "auth", f"user:{request.user.pk}")
        return Response({"detail": "Logged out. Discard tokens client-side."})


@api_view(["GET"])
def me(request):
    return Response(UserSerializer(request.user).data)


ROLE_RANK = {"parent": 1, "coach": 2, "academy_admin": 3, "super_admin": 4}

# Management-level powers: a custom role holding any of these outranks
# operational roles, exactly like an academy admin.
ADMIN_LEVEL_CODES = {"users.manage", "users.create", "users.edit", "roles.manage",
                     "settings.manage", "settings.edit"}

# Platform-level powers a tenant role manager must never hand out or take. Only
# a super admin may grant these; otherwise a custom-role manager could mint a
# peer role that onboards academies or rewrites the whole permission system.
PLATFORM_ONLY_CODES = {"schools.manage", "roles.manage"}


def held_codes(user) -> set:
    if not getattr(user, "role", None):
        return set()
    try:
        return set(user.role.permissions.values_list("code", flat=True))
    except Exception:
        return set()


def assert_may_grant_codes(requester, codes):
    """No privilege escalation: a non-super-admin may only grant permission
    codes their own role already implies (directly or via `<module>.manage` /
    legacy mapping), and never platform-only codes."""
    if is_super_admin(requester):
        return
    from rest_framework.exceptions import PermissionDenied
    from apps.common.permissions import _implies
    held = held_codes(requester)
    for code in codes:
        if code in PLATFORM_ONLY_CODES:
            raise PermissionDenied(f"Only a super admin can grant '{code}'.")
        if not any(_implies(h, code) for h in held):
            raise PermissionDenied(f"You cannot grant '{code}' — it exceeds your own permissions.")


def role_rank(role) -> int:
    """Rank follows power, not just the name, so per-admin custom roles slot
    into the hierarchy: user-management power → academy-admin level,
    anything else limited → operational level."""
    if role is None:
        return 0
    name = role.name if hasattr(role, "name") else str(role)
    if name in ROLE_RANK:
        return ROLE_RANK[name]
    if hasattr(role, "permissions"):
        try:
            codes = set(role.permissions.values_list("code", flat=True))
        except Exception:
            codes = set()
        if codes & ADMIN_LEVEL_CODES:
            return 3
        return 1
    return 3


def requester_rank(user) -> int:
    if user.is_superuser:
        return 99
    return role_rank(getattr(user, "role", None))


def is_super_admin(user) -> bool:
    if user.is_superuser:
        return True
    return getattr(getattr(user, "role", None), "name", None) == "super_admin"


def assert_may_manage(requester, target_user=None, target_role=None, target_school=None):
    """Rank hierarchy: you may only create/edit users strictly below your rank.

    Super admins manage everyone. Academy admins manage coaches/parents —
    only a super admin can create academy admins or touch super admins.
    School-scoped staff additionally cannot reach outside their own school.
    """
    from rest_framework.exceptions import PermissionDenied
    if is_super_admin(requester):
        return
    from apps.common.scope import user_school_id
    own = user_school_id(requester)
    if own is not None:
        want = getattr(target_school, "pk", target_school)
        if want is None and target_user is not None:
            want = getattr(target_user, "school_id", None)
        if want is not None and int(want) != int(own):
            raise PermissionDenied("Outside your school's scope.")
    rr = requester_rank(requester)
    if target_role is not None and role_rank(target_role) >= rr:
        raise PermissionDenied("Only a super admin can assign that role.")
    if target_user is not None and target_user.pk != requester.pk:
        if role_rank(getattr(target_user, "role", None)) >= rr:
            raise PermissionDenied("Only a super admin can modify that account.")


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.select_related("role", "school").all().order_by("-date_joined")
    module = "users"

    def get_permissions(self):
        if self.action in ("list", "retrieve"):
            return [HasPerm("users.view")]
        if self.action == "create":
            return [HasPerm("users.create")]
        if self.action in ("update", "partial_update"):
            return [HasPerm("users.edit")]
        if self.action == "reset_password":
            return [HasPerm("users.reset_password")]
        return [HasPerm("users.manage")]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        return UserSerializer

    filterset_fields = ["role__name", "is_active", "is_archived", "school"]
    search_fields = ["username", "email", "first_name", "last_name", "phone"]
    ordering_fields = ["date_joined", "username"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "school_id")

    def filter_queryset(self, queryset):
        qs = super().filter_queryset(queryset)
        # The account list is an academy-management view: super admins (e.g.
        # the platform owner) are a platform concern and must never appear here.
        # Applies to the list only, so any direct retrieve/update still hits the
        # 403 guard in perform_* rather than a misleading 404.
        if self.action == "list":
            qs = qs.exclude(is_superuser=True).exclude(role__name="super_admin")
        return qs

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        assert_may_manage(self.request.user, target_role=serializer.validated_data.get("role"),
                          target_school=serializer.validated_data.get("school"))
        given = serializer.validated_data.pop("school", None)
        user = serializer.save(school_id=ensure_school(self.request, given))
        # Admin-created accounts must change the (communicated) password at first login.
        user.must_change_password = True
        user.save(update_fields=["must_change_password"])
        log_audit(self.request, "user.create", "users", f"user:{user.pk}",
                  new={"username": user.username, "role": user.role.name if user.role else None})

    def perform_update(self, serializer):
        from rest_framework.exceptions import ValidationError
        obj = self.get_object()
        old = UserSerializer(obj).data
        me = self.request.user.pk == obj.pk
        incoming = serializer.validated_data
        if me:
            if incoming.get("is_active") is False:
                raise ValidationError("You cannot deactivate your own account — ask another administrator.")
            if "role" in incoming and (incoming.get("role") or None) != (obj.role or None):
                raise ValidationError("You cannot change your own role — ask another administrator.")
            if "is_superuser" in incoming and bool(incoming.get("is_superuser")) != bool(obj.is_superuser):
                raise ValidationError("You cannot change your own superuser flag — ask another administrator.")
        else:
            target_sa = bool(obj.is_superuser) or (obj.role and obj.role.name == "super_admin")
            if target_sa and not is_super_admin(self.request.user):
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("Only a super admin can modify a super admin account.")
        if "is_superuser" in incoming and not is_super_admin(self.request.user):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Only a super admin can grant superuser status.")
        assert_may_manage(self.request.user, target_user=obj,
                          target_role=incoming.get("role"),
                          target_school=incoming.get("school", getattr(obj, "school_id", None)))
        user = serializer.save()
        log_audit(self.request, "user.update", "users", f"user:{user.pk}", old=old,
                  new={"username": user.username, "role": user.role.name if user.role else None,
                       "is_active": user.is_active, "is_superuser": user.is_superuser})

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied, ValidationError
        if instance.pk == self.request.user.pk:
            raise ValidationError("You cannot delete your own account.")
        if instance.is_superuser or (instance.role and instance.role.name == "super_admin"):
            raise PermissionDenied("Super admin accounts cannot be deleted. Demote the account first (by another super admin).")
        assert_may_manage(self.request.user, target_user=instance)
        log_audit(self.request, "user.delete", "users", f"user:{instance.pk}",
                  old={"username": instance.username})
        instance.delete()

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("users.reset_password")])
    def reset_password(self, request, pk=None):
        """Super-admin-initiated reset: sets a one-time temporary password.

        The plaintext is returned ONCE (never stored or logged) for HARI to
        share securely. The account must change it at next login.
        """
        import secrets
        import string
        target = self.get_object()
        assert_may_manage(request.user, target_user=target)
        alphabet = string.ascii_letters + string.digits + "!@#$%"
        temp = "".join(secrets.choice(alphabet) for _ in range(12))
        target.set_password(temp)
        target.must_change_password = True
        target.save(update_fields=["password", "must_change_password"])
        log_audit(request, "user.password_reset", "users", f"user:{target.pk}",
                  new={"username": target.username, "must_change_password": True})
        return Response({"detail": "Temporary password set. Share it securely — it is shown only once.",
                         "temporary_password": temp})


class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.prefetch_related("permissions").all()
    serializer_class = RoleSerializer
    module = "roles"
    permission_classes = [HasPerm("users.manage")]

    def get_permissions(self):
        # Role definitions shape everyone's power. Holders of roles.manage may
        # manage CUSTOM roles; system roles and the super_admin role stay
        # super-admin-only (enforced again inside perform_*).
        if self.action in ("list", "retrieve"):
            return [HasPerm("roles.view")]
        if is_super_admin(self.request.user):
            return [IsAuthenticated()]
        return [HasPerm("roles.manage")]

    def _codes(self, role):
        return sorted(role.permissions.values_list("code", flat=True))

    def _guard_system_role(self, role):
        """System roles and the super_admin role are super-admin-only territory."""
        from rest_framework.exceptions import PermissionDenied
        if role.name in ("super_admin", "academy_admin", "coach", "parent"):
            if not is_super_admin(self.request.user):
                raise PermissionDenied("Only a super admin can change system roles.")

    def perform_create(self, serializer):
        assert_may_grant_codes(self.request.user, serializer.validated_data.get("permission_codes") or [])
        role = serializer.save()
        log_audit(self.request, "role.create", "roles", f"role:{role.pk}",
                  new={"name": role.name, "permissions": self._codes(role)})

    def perform_update(self, serializer):
        obj = self.get_object()
        self._guard_system_role(obj)
        old_codes = self._codes(obj)
        codes = serializer.validated_data.get("permission_codes")
        if codes is not None:
            assert_may_grant_codes(self.request.user, codes)
        if "name" in serializer.validated_data and serializer.validated_data["name"] != obj.name:
            if obj.name in ("super_admin", "academy_admin", "coach", "parent"):
                from rest_framework.exceptions import ValidationError
                raise ValidationError("System roles cannot be renamed. Clone them into a custom role instead.")
        serializer.save()
        new_codes = self._codes(obj)
        log_audit(self.request, "role.update", "roles", f"role:{obj.pk}",
                  old={"permissions": old_codes}, new={"name": obj.name, "permissions": new_codes})

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        self._guard_system_role(instance)
        if instance.name in ("super_admin", "academy_admin", "coach", "parent"):
            raise PermissionDenied("System roles cannot be deleted.")
        if instance.users.exists():
            raise PermissionDenied("Role still has member accounts. Move them first.")
        log_audit(self.request, "role.delete", "roles", f"role:{instance.pk}",
                  old={"name": instance.name})
        instance.delete()


class PermissionListView(generics.ListAPIView):
    queryset = Permission.objects.all().order_by("module", "code")
    serializer_class = PermissionSerializer
    permission_classes = [HasPerm("users.manage")]
    pagination_class = None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def change_password(request):
    """Self-service password change. Clears the first-login flag."""
    old = request.data.get("old_password", "")
    new = request.data.get("new_password", "")
    user = request.user
    if not user.check_password(old):
        return Response({"detail": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
    problem = _password_problem(new, user.username, old)
    if problem:
        return Response({"detail": problem}, status=status.HTTP_400_BAD_REQUEST)
    user.set_password(new)
    user.must_change_password = False
    user.save(update_fields=["password", "must_change_password"])
    log_audit(request, "user.password_change", "users", f"user:{user.pk}",
              new={"username": user.username})
    return Response({"detail": "Password changed."})


def _password_problem(new: str, username: str = "", old: str = "") -> str:
    if not new or len(new) < 8:
        return "Password must be at least 8 characters."
    if username and new.lower() == username.lower():
        return "Password must not be the same as the username."
    if old and new == old:
        return "New password must differ from the current password."
    return ""


@api_view(["POST"])
@permission_classes([AllowAny])
def change_password_request(_request):
    # Placeholder: password reset via admin-configured channel. Keep configurable.
    return Response({"detail": "Password reset is handled by an administrator in this release."},
                    status=status.HTTP_501_NOT_IMPLEMENTED)
