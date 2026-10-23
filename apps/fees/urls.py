from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import FeePlanViewSet, StudentFeeViewSet

router = DefaultRouter()
router.register("plans", FeePlanViewSet, basename="fee-plan")
router.register("", StudentFeeViewSet, basename="student-fee")

urlpatterns = [path("", include(router.urls))]
