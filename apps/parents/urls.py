from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import ParentViewSet, StudentParentViewSet

router = DefaultRouter()
router.register("", ParentViewSet, basename="parent")
router.register("links/list", StudentParentViewSet, basename="student-parent")

urlpatterns = [path("", include(router.urls))]
