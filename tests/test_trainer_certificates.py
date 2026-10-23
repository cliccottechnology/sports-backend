"""Trainer certificates: a coach may read back their OWN certificates only.

Regression: the certificate list endpoint required ``coaches.view`` (via
GranularPermissions), which the seeded/migrated ``coach`` role does not hold,
so the Trainer App's "My certificates" list always 403'd even though the
viewset already self-scopes a coach to their own rows. Issuing stays admin-only.
"""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def cert_setup():
    from apps.accounts.models import Role, User
    from apps.coaches.models import Coach, TrainerCertificate
    coach = Coach.objects.create(trainer_id="CERT-T1", first_name="Kiran", phone="7", weekly_off_days=[])
    other = Coach.objects.create(trainer_id="CERT-T2", first_name="Mohan", phone="8", weekly_off_days=[])
    mine = TrainerCertificate.objects.create(
        trainer=coach, title="Mine", period_from="2026-01-01", period_to="2026-03-31",
        sessions_completed=10)
    theirs = TrainerCertificate.objects.create(
        trainer=other, title="Theirs", period_from="2026-01-01", period_to="2026-03-31",
        sessions_completed=5)
    role, _ = Role.objects.get_or_create(name="coach")
    user = User(username="cert_coach", role=role, is_active=True, coach_profile=coach)
    user.set_password("pass12345")
    user.save()
    return {"user": user, "mine": mine, "theirs": theirs}


@pytest.mark.django_db
def test_coach_lists_only_own_certificates(cert_setup):
    c = APIClient()
    c.force_authenticate(cert_setup["user"])
    r = c.get("/api/coaches/certificates/")
    assert r.status_code == 200, r.content
    ids = [x["id"] for x in r.data["results"]]
    assert cert_setup["mine"].id in ids
    assert cert_setup["theirs"].id not in ids


@pytest.mark.django_db
def test_coach_cannot_retrieve_another_coachs_certificate(cert_setup):
    c = APIClient()
    c.force_authenticate(cert_setup["user"])
    r = c.get("/api/coaches/certificates/%s/" % cert_setup["theirs"].id)
    assert r.status_code == 404


@pytest.mark.django_db
def test_coach_cannot_issue_certificate(cert_setup):
    c = APIClient()
    c.force_authenticate(cert_setup["user"])
    r = c.post("/api/coaches/certificates/",
               {"trainer": cert_setup["mine"].trainer_id, "title": "X"}, format="json")
    assert r.status_code == 403
