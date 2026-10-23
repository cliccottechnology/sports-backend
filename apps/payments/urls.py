from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import PaymentViewSet, webhook

router = DefaultRouter()
router.register("", PaymentViewSet, basename="payment")

urlpatterns = [
    path("webhook/", webhook, name="payment-webhook"),
    path("", include(router.urls)),
]
