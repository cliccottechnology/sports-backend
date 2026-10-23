"""Session-based trainer salary: the ten acceptance scenarios, end to end."""
from datetime import date, time, timedelta

import pytest
from rest_framework.test import APIClient


def _mkcoach(tag, first="Coach"):
    from apps.coaches.models import Coach
    return Coach.objects.create(trainer_id=f"S-{tag}", first_name=first, phone=tag, weekly_off_days=[])


def _mkrule(trainer, rate, frm, to=None, **kw):
    from apps.trainer_fees.models import TrainerFeeRule
    return TrainerFeeRule.objects.create(trainer=trainer, rate_per_session=rate,
                                         effective_from=frm, effective_to=to,
                                         is_active=True, **kw)


def _mksessions(n, coach, day0, sport, batch, loc, status="completed"):
    from apps.sessions.models import Session
    out = []
    starts = [time(7, 0), time(9, 0), time(11, 0)]
    for i in range(n):
        day = day0 + timedelta(days=i // len(starts))
        st = starts[i % len(starts)]
        out.append(Session.objects.create(
            sport=sport, batch=batch, coach=coach, date=day,
            status=status, start_time=st, end_time=time(st.hour + 1, 0), location=loc))
    return out


@pytest.fixture
def salary_setup():
    from apps.academy.models import Sport, TrainingLocation
    from apps.batches.models import Batch
    sport = Sport.objects.create(name="S-Football", code="SFB")
    loc = TrainingLocation.objects.create(name="S-Loc")
    coach = _mkcoach("Kumar", "Kumar")
    batch = Batch.objects.create(name="S-B", sport=sport, coach=coach,
                                 start_time=time(7, 0), end_time=time(8, 0), location=loc)
    return {"sport": sport, "loc": loc, "coach": coach, "batch": batch}


@pytest.fixture
def admin_client():
    from apps.accounts.models import User
    u = User(username="sal_admin", is_active=True, is_superuser=True)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    return cl


def _day0():
    return date(2026, 10, 1)


def _gen(cl, coach, frm="2026-10-01", to="2026-10-12"):
    r = cl.post("/api/trainer-fees/payables/generate/",
                {"trainer": coach.pk, "from_date": frm, "to_date": to}, format="json")
    assert r.status_code == 200, r.content
    return r.data


def _earn(cl, coach, frm="2026-10-01", to="2026-10-12"):
    r = cl.get("/api/trainer-fees/payables/earnings/",
               {"trainer": coach.pk, "from": frm, "to": to})
    assert r.status_code == 200, r.content
    return r.data


@pytest.mark.django_db
def test_1_twenty_sessions_make_10000(admin_client, salary_setup):
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(20, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    assert _gen(admin_client, s["coach"])["generated"] == 20
    e = _earn(admin_client, s["coach"])
    assert e["payable_count"] == 20
    assert float(e["payable_amount"]) == 10000.0


@pytest.mark.django_db
def test_2_fifteen_sessions_make_7500(admin_client, salary_setup):
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(15, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    assert float(_earn(admin_client, s["coach"])["payable_amount"]) == 7500.0


@pytest.mark.django_db
def test_3_and_4_only_eligible_count_cancelled_excluded(admin_client, salary_setup):
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(18, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _mksessions(2, s["coach"], date(2026, 10, 20), s["sport"], s["batch"], s["loc"], status="cancelled")
    _gen(admin_client, s["coach"], to="2026-10-31")
    e = _earn(admin_client, s["coach"], to="2026-10-31")
    assert e["payable_count"] == 18
    assert float(e["payable_amount"]) == 9000.0


@pytest.mark.django_db
def test_5_generate_is_idempotent(admin_client, salary_setup):
    from apps.trainer_fees.models import TrainerPayable
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(5, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    assert _gen(admin_client, s["coach"])["generated"] == 5
    assert _gen(admin_client, s["coach"])["generated"] == 5  # same five, none duplicated
    assert TrainerPayable.objects.filter(trainer=s["coach"]).count() == 5
    assert float(_earn(admin_client, s["coach"])["payable_amount"]) == 2500.0


@pytest.mark.django_db
def test_6_rate_change_keeps_history(admin_client, salary_setup):
    from apps.trainer_fees.models import TrainerPayable, TrainerFeeRule
    s = salary_setup
    old = _mkrule(s["coach"], 500, _day0(), to=date(2026, 10, 5))
    _mksessions(5, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    old.is_active = False
    old.save(update_fields=["is_active"])
    _mkrule(s["coach"], 700, date(2026, 10, 6))
    _mksessions(5, s["coach"], date(2026, 10, 6), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    amounts = sorted(float(a) for a in TrainerPayable.objects.filter(
        trainer=s["coach"]).values_list("amount", flat=True))
    assert amounts == [500.0] * 5 + [700.0] * 5
    assert float(_earn(admin_client, s["coach"])["payable_amount"]) == 6000.0


def _verify_all(cl, coach):
    from apps.trainer_fees.models import TrainerPayable
    for p in TrainerPayable.objects.filter(trainer=coach, status="pending"):
        r = cl.post(f"/api/trainer-fees/payables/{p.pk}/verify/", {}, format="json")
        assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_7_partial_payment_leaves_balance(admin_client, salary_setup):
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(20, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    _verify_all(admin_client, s["coach"])
    r = admin_client.post("/api/trainer-fees/payments/",
                          {"trainer": s["coach"].pk, "amount": "6000", "paid_on": "2026-10-12",
                           "reference": "UPI-1", "method": "upi"}, format="json")
    assert r.status_code == 201, r.content
    e = _earn(admin_client, s["coach"])
    assert float(e["paid_amount"]) == 6000.0
    assert float(e["balance"]) == 4000.0


@pytest.mark.django_db
def test_8_fully_paid_cannot_be_paid_again(admin_client, salary_setup):
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(2, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    _verify_all(admin_client, s["coach"])
    r = admin_client.post("/api/trainer-fees/payments/",
                          {"trainer": s["coach"].pk, "amount": "1000", "paid_on": "2026-10-12",
                           "reference": "UPI-2"}, format="json")
    assert r.status_code == 201, r.content
    r = admin_client.post("/api/trainer-fees/payments/",
                          {"trainer": s["coach"].pk, "amount": "100", "paid_on": "2026-10-12",
                           "reference": "UPI-3"}, format="json")
    assert r.status_code == 400, r.content


@pytest.mark.django_db
def test_9_coach_isolation(admin_client, salary_setup):
    from apps.accounts.models import Role, User
    s = salary_setup
    other = _mkcoach("Other", "Other")
    _mkrule(s["coach"], 500, _day0())
    _mkrule(other, 500, _day0())
    _mksessions(3, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _mksessions(1, other, _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    _gen(admin_client, other)
    role, _ = Role.objects.get_or_create(name="coach")
    u = User(username="iso_coach", role=role, is_active=True, coach_profile=other)
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    e = cl.get("/api/trainer-fees/payables/earnings/", {"trainer": s["coach"].pk, "period": "month"})
    assert e.status_code == 200, e.content
    assert e.data["trainer"] == other.pk  # forced to self, never the other trainer
    rows = cl.get("/api/trainer-fees/payables/", {"page_size": 50}).data
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert rows and all(r["trainer"] == other.pk for r in rows)


@pytest.mark.django_db
def test_10_unauthorized_cannot_approve(admin_client, salary_setup):
    from apps.accounts.models import Role, User
    from apps.trainer_fees.models import TrainerPayable
    s = salary_setup
    _mkrule(s["coach"], 500, _day0())
    _mksessions(1, s["coach"], _day0(), s["sport"], s["batch"], s["loc"])
    _gen(admin_client, s["coach"])
    p = TrainerPayable.objects.filter(trainer=s["coach"]).first()
    role, _ = Role.objects.get_or_create(name="coach")
    u = User(username="noverify", role=role, is_active=True, coach_profile=s["coach"])
    u.set_password("pass12345")
    u.save()
    cl = APIClient()
    cl.force_authenticate(u)
    assert cl.post(f"/api/trainer-fees/payables/{p.pk}/verify/", {}, format="json").status_code == 403
    p.refresh_from_db()
    assert p.status == "pending"
