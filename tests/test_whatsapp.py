"""Tests for real-time WhatsApp: Meta template payloads, webhook verify/status, provider status."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def sender():
    from apps.accounts.models import Permission, Role, User
    role, _ = Role.objects.get_or_create(name="wa_sender")
    role.permissions.add(*Permission.objects.filter(code__in=["notifications.send", "whatsapp.send"]))
    u = User(username="wa_user", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    client = APIClient()
    client.force_authenticate(u)
    return client


class FakeResp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {"messages": [{"id": "wamid.TEST123"}]}
        self.text = text or "ok"

    def json(self):
        return self._payload


@pytest.mark.django_db
def test_meta_template_payload(monkeypatch, sender):
    import apps.whatsapp.provider as prov
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "tok")
    calls = {}
    import requests

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return FakeResp()

    monkeypatch.setattr(requests, "post", fake_post)
    p = prov.MetaCloudWhatsApp()
    res = p.send(to_number="919999999999", body="Hi", template_name="fee_reminder",
                 language="en", template_params=["Asha", "2500"])
    assert res.status == "sent"
    assert res.provider_message_id == "wamid.TEST123"
    assert calls["url"].endswith("/12345/messages")
    tpl = calls["json"]["template"]
    assert tpl["name"] == "fee_reminder"
    assert tpl["language"] == {"code": "en"}
    params = tpl["components"][0]["parameters"]
    assert [x["text"] for x in params] == ["Asha", "2500"]


@pytest.mark.django_db
def test_meta_failure_recorded(monkeypatch):
    import apps.whatsapp.provider as prov
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_API_TOKEN", "tok")
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(status_code=400, text="bad template"))
    res = prov.MetaCloudWhatsApp().send(to_number="91", body="x")
    assert res.status == "failed"
    assert "400" in res.failure_info


@pytest.mark.django_db
def test_meta_missing_config(monkeypatch):
    import apps.whatsapp.provider as prov
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_API_TOKEN", raising=False)
    res = prov.MetaCloudWhatsApp().send(to_number="91", body="x")
    assert res.status == "failed"


@pytest.mark.django_db
def test_webhook_verify(monkeypatch):
    from django.test import Client
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "secret123")
    c = Client()
    r = c.get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "secret123",
                                         "hub.challenge": "CHAL"})
    assert r.status_code == 200
    assert r.content == b"CHAL"
    r = c.get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                         "hub.challenge": "CHAL"})
    assert r.status_code == 403


@pytest.mark.django_db
def test_webhook_meta_status_update():
    from apps.whatsapp.models import WhatsAppMessage
    from django.test import Client
    m = WhatsAppMessage.objects.create(to_number="91999", body="hi", provider="meta_cloud",
                                      provider_message_id="wamid.X1", status="sent")
    c = Client()
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.X1", "status": "delivered", "timestamp": "1"}]}}]}]}
    r = c.post("/api/whatsapp/webhook/", payload, content_type="application/json")
    assert r.status_code == 200
    m.refresh_from_db()
    assert m.status == "delivered"


@pytest.mark.django_db
def test_webhook_legacy_format():
    from apps.whatsapp.models import WhatsAppMessage
    from django.test import Client
    m = WhatsAppMessage.objects.create(to_number="91999", body="hi", provider="mock",
                                      provider_message_id="mock_1", status="sent")
    c = Client()
    r = c.post("/api/whatsapp/webhook/", {"provider_message_id": "mock_1", "status": "read"},
               content_type="application/json")
    assert r.status_code == 200
    m.refresh_from_db()
    assert m.status == "read"


@pytest.mark.django_db
def test_provider_status_endpoint(sender):
    r = sender.get("/api/whatsapp/status/")
    assert r.status_code == 200
    assert "provider" in r.data
    assert "live" in r.data


@pytest.mark.django_db
def test_send_with_template_variables(monkeypatch, sender):
    from apps.whatsapp.models import WhatsAppTemplate
    t = WhatsAppTemplate.objects.create(name="fee_reminder", category="fee_reminder",
                                       language="en", body="Hi {{1}}, fee {{2}} due.")
    r = sender.post("/api/whatsapp/messages/",
                    {"to_number": "919999999999", "template": t.pk,
                     "variables": {"1": "Asha", "2": "2500"}}, format="json")
    assert r.status_code == 201, r.content
    assert r.data["body"] == "Hi Asha, fee 2500 due."
    assert r.data["status"] in ("sent", "queued", "failed")


@pytest.mark.django_db
def test_send_rejects_empty_body(sender):
    r = sender.post("/api/whatsapp/messages/", {"to_number": "919999999999", "body": "  "}, format="json")
    assert r.status_code == 400
