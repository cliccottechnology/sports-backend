from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import AttendanceCorrectionViewSet, AttendanceViewSet

router = DefaultRouter()
# NOTE: specific prefixes must be registered before "" — otherwise the
# AttendanceViewSet detail route swallows them (see students/urls.py).
router.register("corrections", AttendanceCorrectionViewSet, basename="attendance-correction")
router.register("", AttendanceViewSet, basename="attendance")

urlpatterns = [path("", include(router.urls))]
