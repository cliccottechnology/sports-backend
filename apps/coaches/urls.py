from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (CoachAssignmentViewSet, CoachAvailabilityViewSet, CoachSportViewSet, CoachViewSet,
                    TrainerAttendanceConfigView, TrainerAttendanceCorrectionViewSet,
                    TrainerCertificateViewSet, TrainerCheckInViewSet, TrainerLeaveViewSet)

router = DefaultRouter()
# NOTE: specific prefixes must be registered before "" — otherwise the
# CoachViewSet detail route swallows them (see students/urls.py).
router.register("checkins", TrainerCheckInViewSet, basename="trainer-checkin")
router.register("leaves", TrainerLeaveViewSet, basename="trainer-leave")
router.register("corrections", TrainerAttendanceCorrectionViewSet, basename="trainer-correction")
router.register("certificates", TrainerCertificateViewSet, basename="trainer-certificate")
router.register("", CoachViewSet, basename="coach")
router.register("sports/list", CoachSportViewSet, basename="coach-sport")
router.register("availability/list", CoachAvailabilityViewSet, basename="coach-availability")
router.register("assignments/list", CoachAssignmentViewSet, basename="coach-assignment")

urlpatterns = [
    path("attendance-config/", TrainerAttendanceConfigView.as_view(), name="trainer-attendance-config"),
    path("", include(router.urls)),
]
