from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(_request):
    return JsonResponse({
        "status": "ok",
        "service": "jetz-backend",
        "vision_provider": getattr(settings, "ATTENDANCE_VISION_PROVIDER", "faceauth"),
        "face_engine": getattr(settings, "FACE_VERIFICATION_PROVIDER", "none"),
    })


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health, name="health"),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/users/", include("apps.accounts.user_urls")),
    path("api/sports/", include("apps.academy.urls")),
    path("api/schools/", include("apps.academy.schools_urls")),
    path("api/modules/", include("apps.academy.modules_urls")),
    path("api/students/", include("apps.students.urls")),
    path("api/parents/", include("apps.parents.urls")),
    path("api/coaches/", include("apps.coaches.urls")),
    path("api/batches/", include("apps.batches.urls")),
    path("api/sessions/", include("apps.sessions.urls")),
    path("api/attendance/", include("apps.attendance.urls")),
    path("api/fees/", include("apps.fees.urls")),
    path("api/payments/", include("apps.payments.urls")),
    path("api/whatsapp/", include("apps.whatsapp.urls")),
    path("api/trainer-fees/", include("apps.trainer_fees.urls")),
    path("api/reports/", include("apps.reports.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
    path("api/audit/", include("apps.audit.urls")),
    path("api/settings/", include("apps.academy.settings_urls")),
    path("api/documents/", include("apps.documents.urls")),
    path("api/support/", include("apps.support.urls")),
    path("api/customization/", include("apps.customization.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
