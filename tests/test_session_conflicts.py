"""Trainer timetable & session conflict prevention.

Covers the authoritative backend rules: coach overlap (all sports/batches),
configurable break buffer, academy daily slot uniqueness, time sanity,
qualification, availability windows, slot suggestions, and that two concurrent
requests can never double-book one trainer. All live records, real PostgreSQL.
"""
import threading
from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def setup_data():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach, CoachSport
    football = Sport.objects.create(name="C-Football", code="CFB")
    cricket = Sport.objects.create(name="C-Cricket", code="CCR")
    loc = TrainingLocation.objects.create(name="C-Loc")
    kumar = Coach.objects.create(trainer_id="C-KUMAR", first_name="Kumar", phone="1", weekly_off_days=[])
    unlinked = Coach.objects.create(trainer_id="C-UNL", first_name="Unlinked", phone="2", weekly_off_days=[])
    windowed = Coach.objects.create(trainer_id="C-WIN", first_name="Windowed", phone="3", weekly_off_days=[5, 6])
    football_only = Coach.objects.create(trainer_id="C-FBO", first_name="Fbo", phone="4", weekly_off_days=[])
    CoachSport.objects.create(coach=kumar, sport=football)
    CoachSport.objects.create(coach=kumar, sport=cricket)
    CoachSport.objects.create(coach=windowed, sport=football)
    CoachSport.objects.create(coach=football_only, sport=football)
    fb_batch = Batch.objects.create(name="C-FB", sport=football, coach=kumar,
                                    start_time=time(7, 0), end_time=time(8, 0), location=loc)
    cr_batch = Batch.objects.create(name="C-CR", sport=cricket, coach=kumar,
                                    start_time=time(9, 0), end_time=time(10, 0), location=loc)
    return {"football": football, "cricket": cricket, "loc": loc, "kumar": kumar,
            "unlinked": unlinked, "windowed": windowed, "football_only": football_only,
            "fb_batch": fb_batch, "cr_batch": cr_batch}


@pytest.fixture
def admin_client():
    from apps.accounts.models import User
    u = User(username="conf_admin", is_active=True, is_superuser=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


def _mk(cl, sport, batch, coach, day, start, end, slot=1, status="scheduled"):
    return cl.post("/api/sessions/", {
        "sport": sport.pk, "batch": batch.pk, "coach": coach.pk,
        "date": day.isoformat(), "start_time": start, "end_time": end,
        "status": status, "slot": slot,
    }, format="json")


@pytest.mark.django_db
def test_1_overlap_blocked_with_specific_message(admin_client, setup_data):
    c = setup_data
    day = date.today() + timedelta(days=3)
    r1 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "16:00", "17:00", slot=1)
    assert r1.status_code == 201, r1.content
    # Same coach, different sport/batch, overlapping window -> rejected with detail.
    r2 = _mk(admin_client, c["cricket"], c["cr_batch"], c["kumar"], day, "16:30", "17:30", slot=2)
    assert r2.status_code == 400, r2.content
    msg = str(r2.data)
    assert "Kumar" in msg and "04:00 PM" in msg and "05:00 PM" in msg
    assert "another available coach" in msg
    # The timetable endpoint renders the day and flags (or not) overlaps.
    tt = admin_client.get("/api/sessions/timetable/",
                          {"date_from": day.isoformat(), "date_to": day.isoformat()})
    assert tt.status_code == 200, tt.content
    row = next(d for d in tt.data["days"] if d["date"] == day.isoformat())
    assert len(row["sessions"]) == 1
    assert row["sessions"][0]["has_conflict"] is False


@pytest.mark.django_db
def test_2_back_to_back_allowed_by_default(admin_client, setup_data):
    c = setup_data
    day = date.today() + timedelta(days=4)
    r1 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "07:00", "08:00", slot=1)
    assert r1.status_code == 201, r1.content
    r2 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "08:00", "09:00", slot=2)
    assert r2.status_code == 201, r2.content


@pytest.mark.django_db
def test_3_break_buffer_enforced_and_suggestions_respect_it(admin_client, setup_data):
    from apps.academy.models import AcademySetting
    c = setup_data
    day = date.today() + timedelta(days=5)
    r1 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "07:00", "08:00", slot=1)
    assert r1.status_code == 201, r1.content
    AcademySetting.objects.create(key="schedule.break_minutes", value=15)
    # 5-minute gap is inside the 15-minute buffer -> rejected.
    r2 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "08:05", "09:05", slot=2)
    assert r2.status_code == 400, r2.content
    assert "already assigned" in str(r2.data)
    # Suggestions skip slots that violate the buffer.
    r = admin_client.get("/api/sessions/suggest_slots/",
                         {"coach": c["kumar"].pk, "date": day.isoformat(), "duration": 60})
    assert r.status_code == 200, r.content
    starts = [s["start_time"] for s in r.data["slots"]]
    assert "08:30" in starts and "07:00" not in starts
    # 08:30 leaves the full buffer -> accepted.
    r3 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "08:30", "09:30", slot=3)
    assert r3.status_code == 201, r3.content


@pytest.mark.django_db
def test_4_academy_daily_slot_is_unique(admin_client, setup_data):
    c = setup_data
    day = date.today() + timedelta(days=6)
    r1 = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "07:00", "08:00", slot=1)
    assert r1.status_code == 201, r1.content
    # Different coach, same date + slot -> the academy's daily slot is taken.
    r2 = _mk(admin_client, c["football"], c["fb_batch"], c["unlinked"], day, "07:00", "08:00", slot=1)
    assert r2.status_code == 400, r2.content
    assert "already scheduled for" in str(r2.data)


@pytest.mark.django_db
def test_5_end_time_must_be_after_start_time(admin_client, setup_data):
    c = setup_data
    day = date.today() + timedelta(days=7)
    r = _mk(admin_client, c["football"], c["fb_batch"], c["kumar"], day, "09:00", "08:00", slot=1)
    assert r.status_code == 400, r.content
    assert "after the start time" in str(r.data)


@pytest.mark.django_db
def test_6_qualification_enforced_when_links_exist(admin_client, setup_data):
    c = setup_data
    day = date.today() + timedelta(days=8)
    # football_only is linked to football only -> booking cricket is rejected.
    r1 = _mk(admin_client, c["cricket"], c["cr_batch"], c["football_only"], day, "09:00", "10:00", slot=1)
    assert r1.status_code == 400, r1.content
    assert "not qualified" in str(r1.data)
    # A coach with no qualification rows stays unconstrained (legacy-friendly).
    r2 = _mk(admin_client, c["cricket"], c["cr_batch"], c["unlinked"], day, "09:00", "10:00", slot=1)
    assert r2.status_code == 201, r2.content


@pytest.mark.django_db
def test_7_availability_window_enforced(admin_client, setup_data):
    from apps.coaches.models import CoachAvailability
    c = setup_data
    monday = date(2026, 10, 12)
    assert monday.weekday() == 0
    CoachAvailability.objects.create(coach=c["windowed"], weekday=0,
                                     start_time=time(7, 0), end_time=time(9, 0), is_available=True)
    # Outside the working window -> rejected.
    r1 = _mk(admin_client, c["football"], c["fb_batch"], c["windowed"], monday, "10:00", "11:00", slot=2)
    assert r1.status_code == 400, r1.content
    assert "only available" in str(r1.data)
    # Inside the window -> accepted.
    r2 = _mk(admin_client, c["football"], c["fb_batch"], c["windowed"], monday, "07:30", "08:30", slot=1)
    assert r2.status_code == 201, r2.content


@pytest.mark.django_db(transaction=True)
def test_8_concurrent_requests_cannot_double_book(admin_client, setup_data):
    from django.db import connections
    from apps.accounts.models import User
    from apps.sessions.models import Session
    c = setup_data
    day = date.today() + timedelta(days=9)
    barrier = threading.Barrier(2)
    results = {}

    def worker(key, start, end, slot):
        cl = APIClient()
        cl.force_authenticate(User.objects.get(username="conf_admin"))
        barrier.wait()
        try:
            r = _mk(cl, c["football"], c["fb_batch"], c["kumar"], day, start, end, slot=slot)
            results[key] = r.status_code
        finally:
            connections.close_all()

    # Different slots (so the daily-slot rule cannot mask the issue) but
    # overlapping times -> only the trainer-conflict lock can save us.
    t1 = threading.Thread(target=worker, args=("a", "16:00", "17:00", 1))
    t2 = threading.Thread(target=worker, args=("b", "16:30", "17:30", 2))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert sorted(results.values()) == [201, 400], results
    assert Session.objects.filter(coach=c["kumar"], date=day, status="scheduled").count() == 1

