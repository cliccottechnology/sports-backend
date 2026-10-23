from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import WhatsAppMessageViewSet, WhatsAppTemplateViewSet, delivery_webhook, provider_status

router = DefaultRouter()
router.register("templates", WhatsAppTemplateViewSet, basename="wa-template")
router.register("messages", WhatsAppMessageViewSet, basename="wa-message")

urlpatterns = [
    path("webhook/", delivery_webhook, name="wa-webhook"),
    path("status/", provider_status, name="wa-status"),
    path("", include(router.urls)),
]
