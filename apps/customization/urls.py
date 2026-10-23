from rest_framework.routers import DefaultRouter

from .views import CustomizationRequestViewSet

router = DefaultRouter()
router.register("requests", CustomizationRequestViewSet, basename="customization-request")

urlpatterns = router.urls
