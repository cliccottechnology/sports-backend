"""Multi-academy platform behaviour: per-academy module enablement enforced on
the API, per-academy settings overriding platform defaults, and branch scoping.
"""
import pytest
from rest_framework.test import APIClient


def mkuser(username, role=None, superuser=False, school=None, pwd="pass12345"):
    from apps.accounts.models import User
    u = User(username=username, role=role, is_active=True, is_superuser=superuser,
             school=school)
    u.set_password(pwd)
    u.save()
    return u


def role_with(*codes, name="trole"):
    from apps.accounts.models import Permission, Role
    r, _ = Role.objects.get_or_create(name=name)
    r.permissions.add(*Permission.objects.filter(code__in=codes))
    return r


def auth_client(user, password="pass12345"):
    cl = APIClient()
    r = cl.post("/api/auth/login/", {"username": user.username, "password": password})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


# Every code a tenant academy admin legitimately needs — full coverage of the
# modules exercised below, with no platform-level (schools.manage) power.
TENANT_CODES = [
    "schools.view", "schools.create", "schools.edit", "schools.archive",
    "sports.view", "students.view", "parents.view", "coaches.view",
    "batches.view", "sessions.view", "attendance.view", "fees.view", "payments.view",
    "trainer_fees.view", "reports.view", "notifications.view", "documents.view",
    "users.view", "settings.view", "settings.edit",
]


@pytest.fixture
def platform_sa():
    from apps.accounts.models import Role
    return mkuser("mt_sa", role=Role.objects.get(name="super_admin"), superuser=True)


@pytest.fixture
def academies():
    from apps.academy.models import School
    a, _ = School.objects.get_or_create(name="Alpha Academy", code="ALPHA")
    b, _ = School.objects.get_or_create(name="Beta Academy", code="BETA")
    return a, b


@pytest.fixture
def alpha_admin(academies):
    a, _b = academies
    return mkuser("mt_alpha", role=role_with(*TENANT_CODES, name="mt_tenant"), school=a)


@pytest.fixture
def beta_admin(academies):
    _a, b = academies
    return mkuser("mt_beta", role=role_with(*TENANT_CODES, name="mt_tenant"), school=b)


def set_module(sa_cl, school_id, module, enabled):
    return sa_cl.put("/api/modules/matrix/", {"school": school_id, "module": module,
                                              "enabled": enabled}, format="json")


@pytest.mark.django_db
def test_module_off_denies_that_academy_only(platform_sa, academies, alpha_admin, beta_admin):
    a, b = academies
    sa = auth_client(platform_sa)

    # Baseline: both tenants reach the fees module.
    assert auth_client(alpha_admin).get("/api/fees/").status_code == 200
    assert auth_client(beta_admin).get("/api/fees/").status_code == 200

    assert set_module(sa, a.pk, "fees", False).status_code == 200, "switch off for Alpha"

    # Alpha loses fees but keeps everything else — and without re-logging in.
    acl = auth_client(alpha_admin)
    assert acl.get("/api/fees/").status_code == 403
    assert acl.get("/api/students/").status_code == 200
    assert acl.get("/api/settings/").status_code == 200

    # Beta and the Super Admin are unaffected.
    assert auth_client(beta_admin).get("/api/fees/").status_code == 200
    assert sa.get("/api/fees/").status_code == 200

    # Switching it back on restores access on the very next call.
    assert set_module(sa, a.pk, "fees", True).status_code == 200
    assert acl.get("/api/fees/").status_code == 200


@pytest.mark.django_db
def test_module_gate_covers_users_and_settings(platform_sa, academies, alpha_admin):
    a, _b = academies
    sa = auth_client(platform_sa)
    acl = auth_client(alpha_admin)

    assert acl.get("/api/users/").status_code == 200
    assert set_module(sa, a.pk, "users", False).status_code == 200
    assert acl.get("/api/users/").status_code == 403

    assert acl.get("/api/settings/").status_code == 200
    assert set_module(sa, a.pk, "settings", False).status_code == 200
    assert acl.get("/api/settings/").status_code == 403

    # Public brand/modules stay reachable even with the settings module off.
    anon = APIClient()
    assert anon.get("/api/settings/brand/").status_code == 200
    assert anon.get("/api/settings/modules/").status_code == 200


@pytest.mark.django_db
def test_module_admin_requires_schools_manage(academies, alpha_admin):
    a, _b = academies
    cl = auth_client(alpha_admin)
    # Tenant admins can read their own switches but never widen them.
    r = cl.get("/api/modules/matrix/")
    assert r.status_code in (200, 403)
    assert set_module(cl, a.pk, "fees", False).status_code == 403
    assert cl.get("/api/modules/catalog/").status_code in (200, 403)


@pytest.mark.django_db
def test_tenant_admin_cannot_onboard_academy(alpha_admin):
    # Holding schools.create/edit (needed to manage their OWN branches) must not
    # let a tenant onboard or rename academies — that stays a platform act.
    cl = auth_client(alpha_admin)
    r = cl.post("/api/schools/", {"name": "Rogue Academy", "code": "ROGUE"}, format="json")
    assert r.status_code == 403, r.content


@pytest.mark.django_db
def test_platform_super_admin_sees_every_academy_matrix(platform_sa, academies):
    a, b = academies
    sa = auth_client(platform_sa)
    r = sa.get("/api/modules/matrix/")
    assert r.status_code == 200, r.content
    names = {row["school"] for row in r.data["rows"]}
    assert {a.pk, b.pk} <= names
    codes = {m["module"] for m in r.data["modules"]}
    assert {"fees", "students", "sessions", "payments"} <= codes


@pytest.mark.django_db
def test_public_modules_endpoint_reflects_academy_switches(platform_sa, academies):
    a, b = academies
    anon = APIClient()

    r = anon.get("/api/settings/modules/")
    assert r.status_code == 200, r.content
    assert all(m["enabled"] for m in r.data["modules"])

    auth_client(platform_sa)
    set_module(auth_client(platform_sa), a.pk, "fees", False)

    by_school = {m["module"]: m["enabled"]
                 for m in anon.get(f"/api/settings/modules/?school={a.pk}").data["modules"]}
    assert by_school["fees"] is False
    assert by_school["students"] is True

    other = {m["module"]: m["enabled"]
             for m in anon.get(f"/api/settings/modules/?school={b.pk}").data["modules"]}
    assert other["fees"] is True


@pytest.mark.django_db
def test_academy_setting_overrides_platform_default(platform_sa, academies, alpha_admin):
    from apps.academy.models import AcademySetting
    a, b = academies
    AcademySetting.objects.update_or_create(school=None, key="academy.name",
                                            defaults={"value": "Platform Name"})
    AcademySetting.objects.update_or_create(school=a, key="academy.name",
                                            defaults={"value": "Alpha Name"})
    AcademySetting.objects.update_or_create(school=b, key="academy.name",
                                            defaults={"value": "Beta Name"})

    anon = APIClient()
    assert anon.get("/api/settings/brand/").data["name"] == "Platform Name"
    assert anon.get(f"/api/settings/brand/?school={a.pk}").data["name"] == "Alpha Name"
    assert anon.get(f"/api/settings/brand/?school={b.pk}").data["name"] == "Beta Name"

    from apps.academy.models import get_setting
    assert get_setting("academy.name", school_id=a.pk) == "Alpha Name"
    assert get_setting("academy.name", school_id=b.pk) == "Beta Name"
    assert get_setting("academy.name") == "Platform Name"


@pytest.mark.django_db
def test_settings_list_scoped_and_writes_stamped(academies, alpha_admin):
    from apps.academy.models import AcademySetting
    a, b = academies
    AcademySetting.objects.update_or_create(school=None, key="academy.email",
                                            defaults={"value": "global@x.test"})
    AcademySetting.objects.update_or_create(school=a, key="academy.email",
                                            defaults={"value": "alpha@x.test"})
    AcademySetting.objects.update_or_create(school=b, key="academy.email",
                                            defaults={"value": "beta@x.test"})

    acl = auth_client(alpha_admin)
    pairs = {(row["key"], row["school"]) for row in acl.get("/api/settings/").data["results"]}
    # Alpha sees the platform default + its own override, never Beta's.
    assert ("academy.email", a.pk) in pairs
    assert ("academy.email", None) in pairs
    assert ("academy.email", b.pk) not in pairs

    r = acl.post("/api/settings/", {"key": "academy.timings", "value": "9-5"},
                 format="json")
    assert r.status_code == 201, r.content
    assert r.data["school"] == a.pk
    # Re-declaring a key it already owns is a duplicate, not a cross-tenant clash.
    assert acl.post("/api/settings/", {"key": "academy.email", "value": "2"},
                    format="json").status_code == 400


@pytest.mark.django_db
def test_branches_are_scoped_and_stamped(academies, alpha_admin, platform_sa):
    a, b = academies
    acl = auth_client(alpha_admin)

    r = acl.post("/api/schools/branches/", {"name": "Alpha North", "code": "AN"},
                 format="json")
    assert r.status_code == 201, r.content
    assert r.data["school"] == a.pk

    seen = {row["name"] for row in acl.get("/api/schools/branches/").data["results"]}
    assert "Alpha North" in seen
    assert "Beta" not in " ".join(seen)

    # A tenant admin cannot point a branch at another academy.
    bad = acl.post("/api/schools/branches/", {"name": "Cross", "school": b.pk},
                   format="json")
    assert bad.status_code == 403, bad.content

    sa = auth_client(platform_sa)
    assert sa.post("/api/schools/branches/", {"name": "Beta North", "school": b.pk},
                   format="json").status_code == 201
    all_names = {row["name"] for row in sa.get("/api/schools/branches/").data["results"]}
    assert {"Alpha North", "Beta North"} <= all_names


@pytest.mark.django_db
def test_same_location_name_allowed_in_two_academies(academies, alpha_admin, platform_sa):
    a, b = academies
    sa = auth_client(platform_sa)
    r1 = sa.post("/api/sports/locations/", {"name": "Main Ground", "school": a.pk},
                 format="json")
    assert r1.status_code == 201, r1.content
    r2 = sa.post("/api/sports/locations/", {"name": "Main Ground", "school": b.pk},
                 format="json")
    assert r2.status_code == 201, r2.content
    # Still unique within a single academy.
    r3 = sa.post("/api/sports/locations/", {"name": "Main Ground", "school": a.pk},
                 format="json")
    assert r3.status_code == 400, r3.content


@pytest.mark.django_db
def test_break_buffer_setting_is_per_academy(academies):
    from apps.academy.models import AcademySetting
    from apps.sessions import services
    a, b = academies
    AcademySetting.objects.create(school=a, key="schedule.break_minutes", value=0)
    AcademySetting.objects.create(school=b, key="schedule.break_minutes", value=30)
    # Each academy resolves its own buffer; a missing row falls back to the
    # platform default rather than another academy's value.
    assert services.get_break_minutes(a.pk) == 0
    assert services.get_break_minutes(b.pk) == 30
    assert services.get_break_minutes() == 0


@pytest.mark.django_db
def test_week_off_setting_is_per_academy(academies):
    """A week off declared for one academy must not close another academy's
    calendar (regression: week_off_days was read unscoped)."""
    from datetime import date, time

    from apps.academy.models import AcademySetting, Sport
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    a, b = academies
    sport = Sport.objects.create(name="MT-Football", code="MTFB")
    coach_a = Coach.objects.create(trainer_id="MT-T1", first_name="C", phone="1",
                                   school=a, weekly_off_days=[])
    batch_a = Batch.objects.create(name="MT-A", sport=sport, coach=coach_a, school=a,
                                   start_time=time(7, 0), end_time=time(8, 0))
    coach_b = Coach.objects.create(trainer_id="MT-T2", first_name="D", phone="2",
                                   school=b, weekly_off_days=[])
    batch_b = Batch.objects.create(name="MT-B", sport=sport, coach=coach_b, school=b,
                                   start_time=time(7, 0), end_time=time(8, 0))

    sunday = date(2026, 10, 11)
    assert sunday.weekday() == 6
    # Week off declared for Beta only.
    AcademySetting.objects.create(school=b, key="academy.week_off_days", value=[6])

    admin_a = mkuser("mt_sched_a", role=role_with("sessions.view", "sessions.create",
                                                  name="mt_sched"), school=a)
    admin_b = mkuser("mt_sched_b", role=role_with("sessions.view", "sessions.create",
                                                  name="mt_sched2"), school=b)

    def payload(batch, coach):
        return {"sport": sport.pk, "batch": batch.pk, "coach": coach.pk,
                "date": sunday.isoformat(), "slot": 1,
                "start_time": "07:00:00", "end_time": "08:00:00", "status": "scheduled"}

    ra = auth_client(admin_a).post("/api/sessions/", payload(batch_a, coach_a), format="json")
    assert ra.status_code == 201, ra.content

    rb = auth_client(admin_b).post("/api/sessions/", payload(batch_b, coach_b), format="json")
    assert rb.status_code == 400, rb.content
    assert "week off" in str(rb.content).lower()


@pytest.mark.django_db
def test_platform_report_available_to_super_admin(platform_sa):
    sa = auth_client(platform_sa)
    r = sa.get("/api/reports/platform/")
    assert r.status_code == 200, r.content
    assert "academies" in r.data or "schools" in r.data or "rows" in r.data

    role = role_with("reports.view", name="mt_reports")
    cl = auth_client(mkuser("mt_reporter", role=role))
    assert cl.get("/api/reports/platform/").status_code == 403


# --- Audit regression tests (multi-tenant leaks and 500s) -------------------

@pytest.mark.django_db
def test_trainer_assignments_list_does_not_error(academies):
    """Regression: filterset_fields referenced a removed field (is_primary),
    which made GET /coaches/assignments/ raise before the view ran (500)."""
    a, _b = academies
    cl = auth_client(mkuser("mt_coachmgr", role=role_with("coaches.view", name="mt_coachmgr"), school=a))
    assert cl.get("/api/coaches/assignments/list/").status_code == 200


@pytest.mark.django_db
def test_student_report_is_school_scoped(academies):
    from datetime import date

    from apps.academy.models import Sport
    from apps.students.models import Student
    a, b = academies
    sport = Sport.objects.create(name="MT-RS", code="MTRS")
    Student.objects.create(student_id="MT-RA", first_name="A", enrollment_date=date.today(),
                           sport=sport, school=a)
    Student.objects.create(student_id="MT-RB", first_name="B", enrollment_date=date.today(),
                           sport=sport, school=b)
    cl = auth_client(mkuser("mt_rpt", role=role_with("reports.view", name="mt_rpt"), school=a))
    r = cl.get("/api/reports/students/")
    assert r.status_code == 200, r.content
    ids = {row["student_id"] for row in r.data["results"]}
    assert "MT-RA" in ids
    assert "MT-RB" not in ids


@pytest.mark.django_db
def test_notifications_are_school_scoped(academies):
    from apps.accounts.models import Permission, Role
    from apps.notifications.models import Notification
    a, b = academies
    parent_role = Role.objects.get_or_create(name="parent")[0]
    target_a = mkuser("mt_nt_a", role=parent_role, school=a)
    target_b = mkuser("mt_nt_b", role=parent_role, school=b)
    Notification.objects.create(user=target_a, title="A", message="to a")
    Notification.objects.create(user=target_b, title="B", message="to b")

    # A tenant admin carries the seeded academy_admin role name (the branch that
    # previously returned every academy's inbox).
    admin_role = Role.objects.get_or_create(name="academy_admin")[0]
    admin_role.permissions.add(*Permission.objects.filter(code="notifications.view"))
    cl = auth_client(mkuser("mt_nt_admin", role=admin_role, school=a))
    r = cl.get("/api/notifications/")
    assert r.status_code == 200, r.content
    rows = r.data.get("results") if isinstance(r.data, dict) else r.data
    titles = {row["title"] for row in rows}
    assert "A" in titles
    assert "B" not in titles


@pytest.mark.django_db
def test_daily_slot_is_per_academy(academies):
    """Regression: the daily slot uniqueness check was global, so one academy
    blocked every other academy from using Session 1 on the same day."""
    from datetime import date, time

    from apps.academy.models import Sport
    from apps.batches.models import Batch
    from apps.coaches.models import Coach
    a, b = academies
    sport = Sport.objects.create(name="MT-SL", code="MTSL")
    monday = date(2026, 10, 12)
    assert monday.weekday() == 0

    def make(school, tag):
        coach = Coach.objects.create(trainer_id=f"MT-SL-{tag}", first_name=tag, phone="1",
                                     school=school, weekly_off_days=[])
        batch = Batch.objects.create(name=f"MT-SL-{tag}", sport=sport, coach=coach, school=school,
                                     start_time=time(7, 0), end_time=time(8, 0))
        return coach, batch

    ca, ba = make(a, "A")
    cb, bb = make(b, "B")
    role = role_with("sessions.view", "sessions.create", name="mt_slot")

    def payload(batch, coach):
        return {"sport": sport.pk, "batch": batch.pk, "coach": coach.pk,
                "date": monday.isoformat(), "slot": 1,
                "start_time": "07:00:00", "end_time": "08:00:00", "status": "scheduled"}

    ra = auth_client(mkuser("mt_slot_a", role=role, school=a)).post("/api/sessions/", payload(ba, ca), format="json")
    assert ra.status_code == 201, ra.content
    rb = auth_client(mkuser("mt_slot_b", role=role, school=b)).post("/api/sessions/", payload(bb, cb), format="json")
    assert rb.status_code == 201, rb.content
