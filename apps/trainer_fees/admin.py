from django.contrib import admin
from .models import TrainerFeeAdjustment, TrainerFeeRule, TrainerPayable, TrainerPayment

admin.site.register(TrainerFeeRule)
admin.site.register(TrainerPayable)
admin.site.register(TrainerPayment)
admin.site.register(TrainerFeeAdjustment)
