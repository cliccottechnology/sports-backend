"""Push notification provider abstraction. Credentials from env only.

Push delivery is reported as CONFIGURED only when a real provider
(is currently Firebase Cloud Messaging) has its credentials in the
environment. Until then, every push send is counted as
"unconfigured/skipped" — the system never claims push delivery.
"""
import os
import uuid
from abc import ABC, abstractmethod


class PushResult:
    def __init__(self, provider_message_id="", status="queued", failure_info=""):
        self.provider_message_id = provider_message_id
        self.status = status
        self.failure_info = failure_info


class BasePush(ABC):
    name = "base"

    @abstractmethod
    def send(self, *, title: str, body: str, user_id: int) -> PushResult:
        """Deliver a push to one user's device(s)."""

    def is_configured(self) -> bool:
        return True


class MockPush(BasePush):
    """Development adapter: records locally, does NOT deliver."""
    name = "mock"

    def is_configured(self) -> bool:
        return False

    def send(self, *, title: str, body: str, user_id: int) -> PushResult:
        return PushResult(provider_message_id=f"mock_{uuid.uuid4().hex[:12]}",
                          status="queued",
                          failure_info="Mock push provider — no delivery.")


class FCMBasePush(BasePush):
    """Firebase Cloud Messaging HTTP v1. Requires service-account
    credentials (FCM_CREDENTIALS / FCM_PROJECT_ID) to be configured."""
    name = "fcm"

    def is_configured(self) -> bool:
        return bool(os.environ.get("FCM_CREDENTIALS") or os.environ.get("FCM_PROJECT_ID"))

    def send(self, *, title: str, body: str, user_id: int) -> PushResult:
        import json
        import requests
        creds = os.environ.get("FCM_CREDENTIALS", "")
        project = os.environ.get("FCM_PROJECT_ID", "")
        if not (creds and project):
            return PushResult(status="failed",
                              failure_info="FCM not configured (FCM_CREDENTIALS / FCM_PROJECT_ID).")
        # Resolve the user's registered device tokens. Tokens are
        # stored on the user profile by a future device-registration
        # flow; with no tokens there is nothing to deliver to.
        from apps.accounts.models import User
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return PushResult(status="failed", failure_info="User not found.")
        tokens = list(getattr(user, "pushtokens", None)
                      and user.pushtokens.values_list("token", flat=True) or [])
        if not tokens:
            return PushResult(status="skipped",
                              failure_info="No device tokens registered for this user.")
        try:
            resp = requests.post(
                f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
                headers={"Authorization": f"Bearer {_access_token(creds)}"},
                json={
                    "message": {
                        "token": tokens[0],
                        "notification": {"title": title, "body": body},
                        "data": {"user_id": str(user_id)},
                    },
                },
                timeout=15,
            )
        except Exception as exc:
            return PushResult(status="failed", failure_info=f"Network error calling FCM: {exc}")
        if resp.status_code >= 400:
            return PushResult(status="failed",
                              failure_info=f"FCM rejected ({resp.status_code}): {resp.text[:500]}")
        try:
            mid = resp.json().get("name", "")
        except Exception:
            mid = ""
        return PushResult(provider_message_id=mid, status="sent")


def _access_token(creds_json: str) -> str:
    """Exchange a service-account JSON for a short-lived OAuth token."""
    import json
    import time
    creds = json.loads(creds_json)
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    scopes = ["https://www.googleapis.com/auth/firebase.messaging"]
    creds_obj = service_account.Credentials.from_service_account_info(creds, scopes=scopes)
    creds_obj.refresh(Request())
    return creds_obj.token


def get_push_provider() -> BasePush:
    provider = os.environ.get("PUSH_PROVIDER", "mock").lower()
    if provider == "fcm":
        return FCMBasePush()
    return MockPush()
