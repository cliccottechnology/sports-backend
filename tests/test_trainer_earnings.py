"""Trainer earnings (week/month work summary for salary) — shared by trainer app and admin."""
from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def earn_setup():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    from apps.trainer_fees.models import TrainerFeeRule, TrainerPayable
    from apps.sessions.models import Session
    sport = Sport.objects.create(name="E-Football", code="EFB")
    c1 = Coach.objects.create(trainer_id="E-T1", first_name="One", phone="1", weekly_off_days=[])
    c2 = Coach.objects.create(trainer_id="E-T2", first_name="Two", phone="2", weekly_off_days=[])
    loc = TrainingLocation.objects.create(name="E-Loc")
    batch = Batch.objects.create(name="E-B", sport=sport, coach=c1,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    rule = TrainerFeeRule.objects.create(trainer=c1, rate_per_session=500,
                                         effective_from=date(2026, 1, 1), is_active=True)
    mon = date.today() - timedelta(days=date.today().weekday())
    s1 = Session.objects.create(sport=sport, batch=batch, coach=c1, date=mon, status="completed",
                                start_time=time(7, 0), end_time=time(8, 0), location=loc)
    s2 = Session.objects.create(sport=sport, batch=batch, coach=c1, date=mon + timedelta(days=2),
                                status="completed", start_time=time(7, 0), end_time=time(8, 0), location=loc)
    s3 = Session.objects.create(sport=sport, batch=batch, coach=c2, date=mon, status="completed",
                                start_time=time(9, 0), end_time=time(10, 0), location=loc)
    TrainerPayable.objects.create(trainer=c1, session=s1, rule=rule, amount=500, status="paid")
    TrainerPayable.objects.create(trainer=c1, session=s2, rule=rule, amount=500, status="pending")
    TrainerPayable.objects.create(trainer=c2, session=s3, rule=rule, amount=500, status="pending")
    return {"c1": c1, "c2": c2, "mon": mon}


@pytest.fixture
def admin_client():
    from apps.accounts.models import User
    u = User(username="earn_admin", is_active=True, is_superuser=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


@pytest.mark.django_db
def test_earnings_week(admin_client, earn_setup):
    c1 = earn_setup["c1"]
    r = admin_client.get("/api/trainer-fees/payables/earnings/",
                         {"trainer": c1.pk, "period": "week"})
    assert r.status_code == 200, r.content
    assert r.data["sessions_taken"] == 2
    assert float(r.data["payable_amount"]) == 500.0
    assert float(r.data["paid_amount"]) == 500.0
    assert len(r.data["by_day"]) == 2
    assert sum(d["sessions"] for d in r.data["by_day"]) == 2


@pytest.mark.django_db
def test_earnings_month_scoped_per_trainer(admin_client, earn_setup):
    c2 = earn_setup["c2"]
    r = admin_client.get("/api/trainer-fees/payables/earnings/",
                         {"trainer": c2.pk, "period": "month"})
    assert r.status_code == 200, r.content
    assert r.data["sessions_taken"] == 1
    assert float(r.data["payable_amount"]) == 500.0
    assert float(r.data["paid_amount"]) == 0.0


@pytest.mark.django_db
def test_earnings_custom_range(admin_client, earn_setup):
    mon = earn_setup["mon"]
    r = admin_client.get("/api/trainer-fees/payables/earnings/",
                         {"from": mon.isoformat(), "to": mon.isoformat()})
    assert r.status_code == 200, r.content
    assert r.data["sessions_taken"] == 2  # both trainers' Monday sessions


@pytest.mark.django_db
def test_generate_reports_skipped_without_rule(admin_client, earn_setup):
    from apps.trainer_fees.models import TrainerFeeRule, TrainerPayable
    TrainerFeeRule.objects.all().delete()
    TrainerPayable.objects.all().delete()
    r = admin_client.post("/api/trainer-fees/payables/generate/", {}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["generated"] == 0
    assert len(r.data["skipped_no_rule"]) == 3
    e = admin_client.get("/api/trainer-fees/payables/earnings/", {"period": "month"})
    assert e.data["sessions_without_payable"] == 3


@pytest.mark.django_db
def test_earnings_coach_auto_scoped(earn_setup):
    from apps.accounts.models import Role, User
    c1 = earn_setup["c1"]
    c2 = earn_setup["c2"]
    role, _ = Role.objects.get_or_create(name="coach")
    u = User(username="earn_coach", role=role, is_active=True, coach_profile=c1)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    # Even asking for the other trainer, a coach only ever sees themselves.
    r = cl.get("/api/trainer-fees/payables/earnings/", {"trainer": c2.pk, "period": "week"})
    assert r.status_code == 200, r.content
    assert r.data["trainer"] == c1.pk
    assert r.data["sessions_taken"] == 2
