"""Payment gateway adapter. Provider chosen via PAYMENT_GATEWAY_PROVIDER env.
Mock provider is for development ONLY and is clearly labelled — never a fake production success."""
import hashlib
import hmac
import os
import time
import uuid
from abc import ABC, abstractmethod


def _body_bytes(raw_body) -> bytes:
    if raw_body is None:
        return b""
    if isinstance(raw_body, (bytes, bytearray)):
        return bytes(raw_body)
    return str(raw_body).encode()


class BaseGateway(ABC):
    name = "base"
    # Webhook signature headers this provider authenticates with, in priority order.
    signature_headers = ("X-Signature",)

    @abstractmethod
    def create_order(self, *, amount, currency, receipt, idempotency_key):
        ...

    @abstractmethod
    def verify_webhook(self, *, payload: dict, signature: str, raw_body: bytes = b"") -> bool:
        ...


class MockGateway(BaseGateway):
    name = "mock"
    signature_headers = ("X-Signature",)

    def create_order(self, *, amount, currency, receipt, idempotency_key):
        return {"order_id": f"mock_order_{uuid.uuid4().hex[:12]}", "amount": str(amount),
                "currency": currency, "receipt": receipt, "dev_only": True}

    def verify_webhook(self, *, payload: dict, signature: str, raw_body: bytes = b"") -> bool:
        # Dev-only shared secret. There is deliberately NO default secret: if
        # MOCK_WEBHOOK_SECRET is not configured verification fails closed, so an
        # unconfigured deployment can never have payment success forged through
        # the well-known default. Signing covers order_id + payment_id.
        secret = os.environ.get("MOCK_WEBHOOK_SECRET", "")
        if not secret:
            return False
        body = payload.get("order_id", "") + payload.get("payment_id", "")
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")


class RazorpayGateway(BaseGateway):
    name = "razorpay"
    signature_headers = ("X-Razorpay-Signature",)

    def create_order(self, *, amount, currency, receipt, idempotency_key):
        import requests
        key = os.environ["RAZORPAY_KEY_ID"]
        secret = os.environ["RAZORPAY_KEY_SECRET"]
        # amount in paise
        resp = requests.post("https://api.razorpay.com/v1/orders",
                             auth=(key, secret),
                             json={"amount": int(float(amount) * 100), "currency": currency, "receipt": receipt},
                             timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {"order_id": data["id"], "amount": str(amount), "currency": currency, "receipt": receipt}

    def verify_webhook(self, *, payload: dict, signature: str, raw_body: bytes = b"") -> bool:
        # Razorpay signs the raw request body with the webhook secret. Fail
        # closed when the secret is missing (never verify against an empty key).
        secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
        if not secret:
            return False
        body = _body_bytes(raw_body)
        if not body:
            body = (payload.get("order_id", "") + "|" + payload.get("payment_id", "")).encode()
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")


class StripeGateway(BaseGateway):
    name = "stripe"
    signature_headers = ("Stripe-Signature",)
    # Stripe rejects events older than its default tolerance (5 minutes).
    tolerance_seconds = 300

    def create_order(self, *, amount, currency, receipt, idempotency_key):
        import requests
        api_key = os.environ["STRIPE_API_KEY"]
        resp = requests.post("https://api.stripe.com/v1/payment_intents",
                             headers={"Authorization": f"Bearer {api_key}"},
                             data={"amount": int(float(amount) * 100), "currency": currency.lower(),
                                   "metadata[receipt]": receipt},
                             timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return {"order_id": data["id"], "amount": str(amount), "currency": currency, "receipt": receipt}

    def verify_webhook(self, *, payload: dict, signature: str, raw_body: bytes = b"") -> bool:
        # Real Stripe verification: the `Stripe-Signature` header carries
        # `t=<timestamp>,v1=<hmac>`, and the HMAC-SHA256 is computed over
        # f"{t}.{raw_body}" with the webhook signing secret. Fail closed on any
        # missing piece — no more "any non-empty signature passes".
        secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        body = _body_bytes(raw_body)
        if not secret or not signature or not body:
            return False
        parts = {}
        for chunk in signature.split(","):
            key, _, value = chunk.partition("=")
            if value:
                parts[key.strip()] = value.strip()
        ts, v1 = parts.get("t", ""), parts.get("v1", "")
        if not ts or not v1:
            return False
        try:
            if abs(time.time() - int(ts)) > self.tolerance_seconds:
                return False
        except (TypeError, ValueError):
            return False
        signed = ts.encode() + b"." + body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, v1)


def get_gateway() -> BaseGateway:
    provider = os.environ.get("PAYMENT_GATEWAY_PROVIDER", "mock").lower()
    if provider == "razorpay":
        return RazorpayGateway()
    if provider == "stripe":
        return StripeGateway()
    return MockGateway()
