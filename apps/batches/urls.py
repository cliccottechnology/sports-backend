from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import BatchStudentViewSet, BatchViewSet

router = DefaultRouter()
router.register("", BatchViewSet, basename="batch")
router.register("rosters/list", BatchStudentViewSet, basename="batch-student")

urlpatterns = [path("", include(router.urls))]
