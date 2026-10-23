from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .modules_views import AcademyModuleViewSet

router = DefaultRouter()
router.register("", AcademyModuleViewSet, basename="academy-module")

urlpatterns = [path("", include(router.urls))]
