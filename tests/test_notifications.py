"""Notification read flows: single + bulk, scoped to the caller."""
import pytest
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_mark_all_read_only_own():
    from apps.accounts.models import User
    from apps.notifications.models import Notification
    a = User(username="nota", is_active=True)
    a.set_password("x")
    a.save()
    b = User(username="notb", is_active=True)
    b.set_password("x")
    b.save()
    Notification.objects.create(user=a, title="A1", message="m")
    Notification.objects.create(user=a, title="A2", message="m")
    Notification.objects.create(user=b, title="B1", message="m")
    c = APIClient()
    c.force_authenticate(a)
    r = c.post("/api/notifications/mark_all_read/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["marked"] == 2
    assert Notification.objects.filter(user=a, is_read=False).count() == 0
    assert Notification.objects.filter(user=b, is_read=False).count() == 1
    one = Notification.objects.filter(user=a).first()
    r2 = c.post(f"/api/notifications/{one.pk}/read/", {}, format="json")
    assert r2.status_code == 200
