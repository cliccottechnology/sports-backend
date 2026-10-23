"""WhatsApp provider abstraction. Credentials from env only. Mock is dev-only."""
import os
import uuid
from abc import ABC, abstractmethod


class SendResult:
    def __init__(self, provider_message_id="", status="queued", failure_info=""):
        self.provider_message_id = provider_message_id
        self.status = status
        self.failure_info = failure_info


class BaseWhatsApp(ABC):
    name = "base"

    @abstractmethod
    def send(self, *, to_number: str, body: str, template_name: str | None = None,
             language: str = "en", template_params: list | None = None) -> SendResult:
        """Send a message. Template sends (Meta-approved name + params) deliver
        outside the 24h service window; plain body text only works inside it."""

    def is_configured(self) -> bool:
        return True


class MockWhatsApp(BaseWhatsApp):
    name = "mock"

    def send(self, *, to_number: str, body: str, template_name: str | None = None,
             language: str = "en", template_params: list | None = None) -> SendResult:
        # Development adapter: records message locally, does NOT deliver.
        return SendResult(provider_message_id=f"mock_{uuid.uuid4().hex[:12]}", status="sent")


class MetaCloudWhatsApp(BaseWhatsApp):
    """Meta WhatsApp Cloud API. Business-initiated messages MUST use an
    approved template (name + language + body params); free text only
    delivers inside the 24h customer-service window."""
    name = "meta_cloud"

    def is_configured(self) -> bool:
        return bool(os.environ.get("WHATSAPP_API_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))

    def send(self, *, to_number: str, body: str, template_name: str | None = None,
             language: str = "en", template_params: list | None = None) -> SendResult:
        import requests
        base = os.environ.get("WHATSAPP_API_BASE_URL", "https://graph.facebook.com/v20.0").rstrip("/")
        try:
            phone_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
            token = os.environ["WHATSAPP_API_TOKEN"]
        except KeyError as exc:
            return SendResult(status="failed", failure_info=f"Missing configuration: {exc}. Set WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_API_TOKEN.")
        if template_name:
            payload = {
                "messaging_product": "whatsapp", "to": to_number, "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language or "en"},
                    "components": [{"type": "body", "parameters": [
                        {"type": "text", "text": str(p)} for p in (template_params or [])]}],
                },
            }
        else:
            payload = {"messaging_product": "whatsapp", "to": to_number,
                       "type": "text", "text": {"preview_url": False, "body": body}}
        try:
            resp = requests.post(f"{base}/{phone_id}/messages",
                                 headers={"Authorization": f"Bearer {token}"},
                                 json=payload, timeout=20)
        except Exception as exc:
            return SendResult(status="failed", failure_info=f"Network error calling Meta: {exc}")
        if resp.status_code >= 400:
            return SendResult(status="failed", failure_info=f"Meta rejected ({resp.status_code}): {resp.text[:1000]}")
        try:
            mid = resp.json()["messages"][0]["id"]
        except Exception:
            mid = ""
        return SendResult(provider_message_id=mid, status="sent")


class TwilioWhatsApp(BaseWhatsApp):
    name = "twilio"

    def is_configured(self) -> bool:
        import os as _os
        return bool(_os.environ.get("TWILIO_ACCOUNT_SID") and _os.environ.get("TWILIO_AUTH_TOKEN")
                    and _os.environ.get("TWILIO_WHATSAPP_FROM"))

    def send(self, *, to_number: str, body: str, template_name: str | None = None,
             language: str = "en", template_params: list | None = None) -> SendResult:
        import requests
        from requests.auth import HTTPBasicAuth
        sid = os.environ["TWILIO_ACCOUNT_SID"]
        token = os.environ["TWILIO_AUTH_TOKEN"]
        sender = os.environ["TWILIO_WHATSAPP_FROM"]
        resp = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                             auth=HTTPBasicAuth(sid, token),
                             data={"From": sender, "To": f"whatsapp:{to_number}", "Body": body}, timeout=15)
        if resp.status_code >= 400:
            return SendResult(status="failed", failure_info=resp.text[:1000])
        return SendResult(provider_message_id=resp.json().get("sid", ""), status="sent")


def get_whatsapp_provider() -> BaseWhatsApp:
    provider = os.environ.get("WHATSAPP_PROVIDER", "mock").lower()
    if provider == "meta_cloud":
        return MetaCloudWhatsApp()
    if provider == "twilio":
        return TwilioWhatsApp()
    return MockWhatsApp()
