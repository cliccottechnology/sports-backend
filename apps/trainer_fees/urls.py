from django.urls import include, path
from rest_framework.routers import DefaultRouter
from .views import (TrainerFeeAdjustmentViewSet, TrainerFeeRuleViewSet, TrainerPayableViewSet,
                    TrainerPaymentViewSet, TrainerSalaryConfigViewSet)

router = DefaultRouter()
router.register("rules", TrainerFeeRuleViewSet, basename="trainer-rule")
router.register("configs", TrainerSalaryConfigViewSet, basename="trainer-salary-config")
router.register("payables", TrainerPayableViewSet, basename="trainer-payable")
router.register("payments", TrainerPaymentViewSet, basename="trainer-payment")
router.register("adjustments", TrainerFeeAdjustmentViewSet, basename="trainer-adjustment")

urlpatterns = [path("", include(router.urls))]
