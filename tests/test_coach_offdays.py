"""Trainer week-offs are manual (any 2 days) — never auto Sat/Sun. Covers self-service + validation."""
import pytest
from rest_framework.test import APIClient


@pytest.fixture
def coach_client():
    from apps.accounts.models import User
    from apps.coaches.models import Coach
    u = User(username="offday_coach", is_active=True)
    u.set_password("pass12345")
    u.save()
    c = Coach.objects.create(trainer_id="TRN-OFF", first_name="Off", phone="1", weekly_off_days=[5, 6])
    u.coach_profile = c
    u.save()
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": "offday_coach", "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


@pytest.mark.django_db
def test_set_my_off_days_ok(coach_client):
    r = coach_client.post("/api/coaches/set_my_off_days/", {"weekly_off_days": [0, 3]}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["weekly_off_days"] == [0, 3]


@pytest.mark.django_db
def test_set_my_off_days_rejects_bad(coach_client):
    assert coach_client.post("/api/coaches/set_my_off_days/", {"weekly_off_days": [1]}, format="json").status_code == 400
    assert coach_client.post("/api/coaches/set_my_off_days/", {"weekly_off_days": [2, 2]}, format="json").status_code == 400
    assert coach_client.post("/api/coaches/set_my_off_days/", {"weekly_off_days": [0, 7]}, format="json").status_code == 400
    assert coach_client.post("/api/coaches/set_my_off_days/", {}, format="json").status_code == 400


@pytest.mark.django_db
def test_coach_serializer_validates_off_days():
    from apps.coaches.views import CoachSerializer
    s = CoachSerializer(data={"trainer_id": "X1", "first_name": "A", "phone": "1", "weekly_off_days": [1]})
    assert not s.is_valid()
    s = CoachSerializer(data={"trainer_id": "X1", "first_name": "A", "phone": "1", "weekly_off_days": [1, 4]})
    assert s.is_valid(), s.errors


@pytest.mark.django_db
def test_update_my_profile_contact_fields():
    from apps.accounts.models import User
    from apps.coaches.models import Coach
    from rest_framework.test import APIClient
    coach = Coach.objects.create(trainer_id="TRN-EDIT", first_name="Ed", phone="111", weekly_off_days=[5, 6])
    u = User(username="edit_coach", is_active=True, first_name="Ed")
    u.set_password("pass12345")
    u.save()
    u.coach_profile = coach
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    r = cl.patch("/api/coaches/update_my_profile/",
                 {"phone": "9998887777", "email": "ed@example.com",
                  "trainer_id": "HACKED", "is_active": False}, format="json")
    assert r.status_code == 200, r.content
    coach.refresh_from_db()
    assert coach.phone == "9998887777"
    assert coach.email == "ed@example.com"
    assert coach.trainer_id == "TRN-EDIT"  # admin-managed: ignored
    assert coach.is_active is True
    u.refresh_from_db()
    assert u.phone == "9998887777"


@pytest.mark.django_db
def test_update_my_profile_needs_coach_link():
    from apps.accounts.models import User
    from rest_framework.test import APIClient
    u = User(username="nolink", is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    r = cl.patch("/api/coaches/update_my_profile/", {"phone": "1"}, format="json")
    assert r.status_code == 404


@pytest.mark.django_db
def test_update_my_profile_rejects_bad_email():
    from apps.accounts.models import User
    from apps.coaches.models import Coach
    from rest_framework.test import APIClient
    coach = Coach.objects.create(trainer_id="TRN-EDIT2", first_name="Ed", phone="1", weekly_off_days=[])
    u = User(username="edit_coach2", is_active=True)
    u.set_password("pass12345")
    u.save()
    u.coach_profile = coach
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    r = cl.patch("/api/coaches/update_my_profile/", {"email": "not-an-email"}, format="json")
    assert r.status_code == 400


def test_update_my_profile_requires_login():
    from rest_framework.test import APIClient
    r = APIClient().patch("/api/coaches/update_my_profile/", {"phone": "1"}, format="json")
    assert r.status_code in (401, 403)


@pytest.mark.django_db
def test_admin_coach_update_syncs_login_user():
    from apps.accounts.models import Permission, Role, User
    from apps.coaches.models import Coach
    perm, _ = Permission.objects.get_or_create(code="coaches.manage",
                                              defaults={"name": "m", "module": "coaches"})
    role, _ = Role.objects.get_or_create(name="coach_admin")
    role.permissions.add(perm)
    admin = User(username="coach_admin", role=role, is_active=True)
    admin.set_password("pass12345")
    admin.save()
    coach = Coach.objects.create(trainer_id="TRN-SYNC", first_name="Old", phone="1", weekly_off_days=[])
    u = User(username="sync_coach", is_active=True, first_name="Old")
    u.set_password("pass12345")
    u.save()
    u.coach_profile = coach
    u.save()
    from rest_framework.test import APIClient
    cl = APIClient()
    cl.force_authenticate(admin)
    r = cl.patch(f"/api/coaches/{coach.pk}/", {"first_name": "New", "phone": "222"}, format="json")
    assert r.status_code == 200, r.content
    u.refresh_from_db()
    assert u.first_name == "New"
    assert u.phone == "222"
