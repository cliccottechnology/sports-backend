from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import BranchViewSet, SchoolViewSet

router = DefaultRouter()
router.register("branches", BranchViewSet, basename="branch")
router.register("", SchoolViewSet, basename="school")

urlpatterns = [path("", include(router.urls))]
