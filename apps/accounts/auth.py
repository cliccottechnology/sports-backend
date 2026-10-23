"""JWT authentication that keeps working when it matters — and stops when it must.

simplejwt only checks ``is_active`` at login time. A deactivated user keeps
full API access until their token expires. That breaks the admin promise that
disabling an account takes effect everywhere, immediately. This subclass
re-checks ``is_active`` on every request.

It also enforces an absolute session cap (``settings.JWT_SESSION_MINUTES``,
default 12 hours): every access token issued by a session login carries
``session_exp`` (unix timestamp); any request past that instant is rejected
with 401 even if the token itself has not yet expired. Refresh tokens carry
the same claim and the refresh endpoint refuses to extend past it. No sliding,
no silent extension within a session.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed, InvalidToken
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

SESSION_MINUTES_DEFAULT = 720

# Identity claims carried on every session token so each app/portal has a
# self-describing, server-verified identity. Refreshed access tokens keep them
# (see SessionRefreshToken.access_token). Never trust a role the client sends.
IDENTITY_CLAIMS = ("role", "portal", "parent_id")


def session_expiry_timestamp(from_time=None):
    base = from_time or timezone.now()
    minutes = int(getattr(settings, "JWT_SESSION_MINUTES", SESSION_MINUTES_DEFAULT))
    return int((base + timedelta(minutes=minutes)).timestamp())


class SessionRefreshToken(RefreshToken):
    """Refresh token carrying the absolute session deadline and identity on
    derived access tokens. simplejwt's default only copies ``no_copy_claims``;
    we forward every identity claim so a refreshed access token still knows who
    it belongs to and which portal issued it."""

    @property
    def access_token(self):
        access = super().access_token
        for claim in ("session_exp",) + IDENTITY_CLAIMS:
            if claim in self:
                access[claim] = self[claim]
        return access


class SessionTokenObtainPairSerializer(TokenObtainPairSerializer):
    token_class = SessionRefreshToken

    # Role-scoped subclasses set these. ``portal`` labels the issuing app
    # (admin/super/trainer/parent); ``required_role`` enforces, server-side,
    # that only that role may authenticate through this endpoint.
    portal = None
    required_role = None

    @classmethod
    def identity_claims(cls, user) -> dict:
        role = getattr(user, "role", None)
        claims = {"role": role.name if role else None}
        parent_id = getattr(user, "parent_profile_id", None)
        if parent_id:
            claims["parent_id"] = parent_id
        return claims

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["session_exp"] = session_expiry_timestamp()
        for key, value in cls.identity_claims(user).items():
            token[key] = value
        if cls.portal:
            token["portal"] = cls.portal
        return token

    def validate(self, attrs):
        data = super().validate(attrs)
        user = getattr(self, "user", None)
        if self.required_role is not None:
            role_name = getattr(getattr(user, "role", None), "name", None)
            if role_name != self.required_role:
                raise PermissionDenied(
                    f"This login is for {self.required_role} accounts only."
                )
        try:
            data["session_exp"] = int(self.token_class(data["refresh"]).get("session_exp") or 0) or None
        except Exception:
            data["session_exp"] = None
        if self.portal:
            data["portal"] = self.portal
            data["role"] = getattr(getattr(user, "role", None), "name", None)
        return data


class TrainerLoginSerializer(SessionTokenObtainPairSerializer):
    """Dedicated trainer/coach login: the backend verifies the assigned role."""

    portal = "trainer"
    required_role = "coach"


class _IdentifierMixin:
    """Resolve a login identifier that may be a username, email, or phone.

    Parents sign in with their registered phone (or username/email); the exact
    same credential check still runs in ``super().validate`` — we only map the
    identifier to the canonical username first. Returns None when nothing
    matches, leaving the standard invalid-credentials response in place.
    """

    @staticmethod
    def _resolve_identifier(identifier: str):
        if not identifier:
            return None
        User = get_user_model()
        user = (
            User.objects.filter(
                Q(username__iexact=identifier) | Q(email__iexact=identifier) | Q(phone=identifier)
            )
            .only("username")
            .first()
        )
        return user.get_username() if user else None

    def validate(self, attrs):
        identifier = attrs.get(self.username_field) or attrs.get("username") or attrs.get("phone") or ""
        resolved = self._resolve_identifier(identifier)
        if resolved:
            attrs = dict(attrs)
            attrs[self.username_field] = resolved
        return super().validate(attrs)


class ParentLoginSerializer(_IdentifierMixin, SessionTokenObtainPairSerializer):
    """Dedicated parent login: phone/username/email + password, role-verified."""

    portal = "parent"
    required_role = "parent"


class SessionTokenRefreshSerializer(TokenRefreshSerializer):
    token_class = SessionRefreshToken

    def validate(self, attrs):
        raw = attrs.get("refresh", "")
        try:
            incoming = self.token_class(raw)
            exp = int(incoming.get("session_exp") or 0)
        except Exception:
            exp = 0
        if not exp:
            # Pre-rollout tokens carry no deadline: force a fresh login once.
            raise InvalidToken("Session has no deadline. Please log in again.")
        if timezone.now().timestamp() > exp:
            raise InvalidToken("Session expired. Please log in again.")
        data = super().validate(attrs)
        data["session_exp"] = exp
        return data


class ActiveJWTAuthentication(JWTAuthentication):
    def get_user(self, validated_token):
        exp = validated_token.get("session_exp") if hasattr(validated_token, "get") else None
        if exp and timezone.now().timestamp() > int(exp):
            raise AuthenticationFailed("Session expired. Please log in again.", code="session_expired")
        user = super().get_user(validated_token)
        if user is not None and not user.is_active:
            raise AuthenticationFailed("Account is disabled.", code="account_disabled")
        if user is not None and getattr(user, "school_id", None):
            from apps.academy.models import School
            if not School.objects.filter(pk=user.school_id, is_active=True).exists():
                raise AuthenticationFailed("School is deactivated.", code="school_disabled")
        return user
