"""Coach self-service profile: photo upload validation + scoping."""
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient


def _img(name="c.jpg"):
    buf = BytesIO()
    Image.new("RGB", (300, 300), "navy").save(buf, format="JPEG")
    buf.seek(0)
    return SimpleUploadedFile(name, buf.read(), content_type="image/jpeg")


@pytest.fixture
def coach_user():
    from datetime import date
    from apps.academy.models import Sport
    from apps.accounts.models import Permission, Role, User
    from apps.coaches.models import Coach
    sport = Sport.objects.create(name="C-Skating", code="CSK")
    coach = Coach.objects.create(trainer_id="C-T1", first_name="Kavya", phone="5", weekly_off_days=[])
    perm, _ = Permission.objects.get_or_create(code="attendance.mark",
                                               defaults={"name": "mark", "module": "attendance"})
    role, _ = Role.objects.get_or_create(name="coach")
    role.permissions.add(perm)
    u = User(username="self_coach", role=role, is_active=True, coach_profile=coach)
    u.set_password("pass12345")
    u.save()
    return u


@pytest.mark.django_db
def test_coach_can_upload_own_photo(coach_user):
    c = APIClient()
    c.force_authenticate(coach_user)
    r = c.patch("/api/coaches/update_my_profile/", {"photo": _img()}, format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["coach"]["photo"]
    assert r.data["changed"] == {"photo": r.data["coach"]["photo"].split("/media/")[-1]} or "photo" in r.data["changed"]


@pytest.mark.django_db
def test_coach_photo_rejects_non_image(coach_user):
    c = APIClient()
    c.force_authenticate(coach_user)
    bad = SimpleUploadedFile("x.txt", b"nope", content_type="text/plain")
    r = c.patch("/api/coaches/update_my_profile/", {"photo": bad}, format="multipart")
    assert r.status_code == 400


@pytest.mark.django_db
def test_update_profile_ignores_privileged_fields(coach_user):
    c = APIClient()
    c.force_authenticate(coach_user)
    r = c.patch("/api/coaches/update_my_profile/",
                {"first_name": "Kavya", "is_active": False, "trainer_id": "HACK"},
                format="json")
    assert r.status_code == 200, r.content
    coach_user.coach_profile.refresh_from_db()
    assert coach_user.coach_profile.trainer_id == "C-T1"
    assert coach_user.coach_profile.is_active is True
