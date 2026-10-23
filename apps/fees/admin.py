from django.contrib import admin
from .models import FeeAdjustment, FeePlan, StudentFee

admin.site.register(FeePlan)
admin.site.register(StudentFee)
admin.site.register(FeeAdjustment)
