"""Academy week offs (Settings → academy.week_off_days) block new sessions on closed days."""
from datetime import date, time

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def admin_client():
    from apps.accounts.models import Permission, Role, User
    perm, _ = Permission.objects.get_or_create(code="sessions.manage",
                                              defaults={"name": "manage", "module": "sessions"})
    role, _ = Role.objects.get_or_create(name="sched_admin")
    role.permissions.add(perm)
    u = User(username="sched_admin", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.fixture
def sched_setup():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    sport = Sport.objects.create(name="WO-Football", code="WOFB")
    coach = Coach.objects.create(trainer_id="WO-T1", first_name="W", phone="1", weekly_off_days=[0, 1])
    loc = TrainingLocation.objects.create(name="WO-Loc")
    batch = Batch.objects.create(name="WO-B", sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    return {"sport": sport, "coach": coach, "batch": batch, "loc": loc}


def _payload(setup, day, slot=1):
    return {"sport": setup["sport"].pk, "batch": setup["batch"].pk, "coach": setup["coach"].pk,
            "date": day.isoformat(), "slot": slot,
            "start_time": "07:00:00", "end_time": "08:00:00", "status": "scheduled"}


@pytest.mark.django_db
def test_academy_week_off_blocks_scheduling(admin_client, sched_setup):
    from apps.academy.models import AcademySetting
    AcademySetting.objects.create(key="academy.week_off_days", value=[6])  # Sundays closed
    sunday = date(2026, 10, 11)
    assert sunday.weekday() == 6
    r = admin_client.post("/api/sessions/", _payload(sched_setup, sunday), format="json")
    assert r.status_code == 400, r.content
    assert "week off" in str(r.content).lower()


@pytest.mark.django_db
def test_open_day_schedules_fine(admin_client, sched_setup):
    from apps.academy.models import AcademySetting
    AcademySetting.objects.create(key="academy.week_off_days", value=[6])
    wed = date(2026, 10, 14)
    r = admin_client.post("/api/sessions/", _payload(sched_setup, wed), format="json")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_no_setting_no_block(admin_client, sched_setup):
    sunday = date(2026, 10, 11)
    r = admin_client.post("/api/sessions/", _payload(sched_setup, sunday), format="json")
    assert r.status_code == 201, r.content


@pytest.mark.django_db
def test_unrelated_edit_not_blocked(admin_client, sched_setup):
    from apps.academy.models import AcademySetting
    from apps.sessions.models import Session
    wed = date(2026, 10, 14)
    s = Session.objects.create(sport=sched_setup["sport"], batch=sched_setup["batch"],
                               coach=sched_setup["coach"], date=wed,
                               start_time=time(7, 0), end_time=time(8, 0))
    AcademySetting.objects.create(key="academy.week_off_days", value=[wed.weekday()])
    r = admin_client.patch(f"/api/sessions/{s.pk}/", {"notes": "hello"}, format="json")
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_explicit_override_allows_off_day(admin_client, sched_setup):
    from apps.academy.models import AcademySetting
    AcademySetting.objects.create(key="academy.week_off_days", value=[6])
    sunday = date(2026, 10, 11)
    payload = _payload(sched_setup, sunday)
    payload["allow_off_day"] = True
    r = admin_client.post("/api/sessions/", payload, format="json")
    assert r.status_code == 201, r.content
