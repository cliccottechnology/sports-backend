from django.urls import path
from . import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("platform/", views.platform, name="r-platform"),
    path("charts/", views.charts, name="r-charts"),
    path("students/", views.student_master, name="r-students"),
    path("fees/collection/", views.fee_collection, name="r-fee-collection"),
    path("fees/pending-overdue/", views.pending_overdue, name="r-pending"),
    path("attendance/", views.attendance_report, name="r-attendance"),
    path("attendance/makeup/", views.makeup_report, name="r-makeup"),
    path("sessions/", views.session_report, name="r-sessions"),
    path("trainer-fees/", views.trainer_fee_report, name="r-trainer-fees"),
    path("payments/", views.payment_history, name="r-payments"),
    path("whatsapp/", views.whatsapp_report, name="r-whatsapp"),
    path("audit/", views.audit_report, name="r-audit"),
    path("coach-allocation/", views.coach_allocation, name="r-coach"),
]
