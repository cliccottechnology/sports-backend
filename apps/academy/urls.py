from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (AcademyAchievementViewSet, AcademySettingViewSet, SessionTypeViewSet, SportCategoryViewSet,
                    SportViewSet, TrainingLocationViewSet)

router = DefaultRouter()
router.register("sports", SportViewSet, basename="sport")
router.register("achievements", AcademyAchievementViewSet, basename="achievement")
router.register("categories", SportCategoryViewSet, basename="sport-category")
router.register("locations", TrainingLocationViewSet, basename="location")
router.register("session-types", SessionTypeViewSet, basename="session-type")

urlpatterns = [path("", include(router.urls))]
