from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import NotificationTemplateViewSet, NotificationViewSet

router = DefaultRouter()
router.register("", NotificationViewSet, basename="notification")
router.register("templates/list", NotificationTemplateViewSet, basename="notification-template")

urlpatterns = [path("", include(router.urls))]
