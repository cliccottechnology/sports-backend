"""Every sidebar module's key endpoints must resolve (no dead buttons).

Unauthenticated hits must fail with auth errors (401/403/405) — never 404,
which would mean the frontend is calling a URL the backend doesn't serve.
"""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def anon():
    return APIClient()


@pytest.mark.django_db
@pytest.mark.parametrize("method,url", [
    ("get", "/api/sports/sports/"),
    ("get", "/api/students/"),
    ("get", "/api/students/summary/"),
    ("get", "/api/parents/"),
    ("get", "/api/parents/links/list/"),
    ("get", "/api/batches/"),
    ("get", "/api/batches/rosters/list/"),
    ("get", "/api/sessions/"),
    ("get", "/api/attendance/roster/"),
    ("get", "/api/coaches/"),
    ("get", "/api/coaches/availability/list/"),
    ("get", "/api/documents/"),
    ("get", "/api/fees/"),
    ("get", "/api/fees/plans/"),
    ("get", "/api/payments/"),
    ("get", "/api/trainer-fees/payables/"),
    ("get", "/api/trainer-fees/rules/"),
    ("get", "/api/reports/dashboard/"),
    ("get", "/api/reports/sessions/"),
    ("get", "/api/whatsapp/messages/"),
    ("get", "/api/whatsapp/templates/"),
    ("get", "/api/notifications/"),
    ("get", "/api/audit/logs/"),
    ("get", "/api/users/"),
    ("get", "/api/users/roles/"),
    ("get", "/api/settings/"),
    ("get", "/api/settings/brand/"),
    ("post", "/api/notifications/mark_all_read/"),
    ("post", "/api/attendance/bulk_mark/"),
])


def test_module_endpoints_resolve(anon, method, url):
    r = getattr(anon, method)(url, {}, format="json")
    assert r.status_code != 404, f"{method.upper()} {url} does not resolve"


@pytest.mark.django_db
def test_brand_is_public_and_live(anon):
    """Login screens + trainer app need the name with no credentials, reflecting renames."""
    from apps.academy.models import AcademySetting
    r = anon.get("/api/settings/brand/")
    assert r.status_code == 200, r.content
    assert r.data["name"] == ""
    AcademySetting.objects.update_or_create(key="academy.name", defaults={"value": "Victory Sports Hub"})
    r = anon.get("/api/settings/brand/")
    assert r.status_code == 200, r.content
    assert r.data["name"] == "Victory Sports Hub"


@pytest.mark.django_db
def test_brand_fallbacks_are_not_hardcoded(anon):
    """Unconfigured installs must not advertise a built-in academy identity."""
    r = anon.get("/api/settings/brand/")
    assert r.status_code == 200, r.content
    assert "JETZ" not in (r.data["name"] or "")
    assert r.data["tagline"] == ""


@pytest.mark.django_db
def test_brand_ignores_stale_token(anon):
    """A stale/expired token must not break the public brand lookup.

    The frontend attaches its current Bearer token to every request. DRF
    authenticates (and can 401) before permission checks, so without skipping
    auth on this public action a stale token would reject it."""
    r = anon.get("/api/settings/brand/", HTTP_AUTHORIZATION="Bearer not-a-real-token")
    assert r.status_code == 200, r.content
    assert "name" in r.data


@pytest.mark.django_db
def test_brand_exposes_only_whitelisted_public_contact_details(anon):
    """The parent app shows academy contact info without logging in; the public
    endpoint must expose just the identity/contact whitelist, never other settings."""
    from apps.academy.models import AcademySetting
    AcademySetting.objects.update_or_create(key="academy.phone", defaults={"value": "9000000000"})
    AcademySetting.objects.update_or_create(key="academy.email", defaults={"value": "hello@academy.test"})
    AcademySetting.objects.update_or_create(key="academy.address", defaults={"value": "1 Stadium Road"})
    AcademySetting.objects.update_or_create(key="internal.secret_key", defaults={"value": "topsecret-value"})

    r = anon.get("/api/settings/brand/")
    assert r.status_code == 200, r.content
    assert r.data["phone"] == "9000000000"
    assert r.data["email"] == "hello@academy.test"
    assert r.data["address"] == "1 Stadium Road"
    assert "secret_key" not in r.data
    assert "topsecret-value" not in str(r.data)
