from django.contrib import admin
from .models import Coach, CoachAssignment, CoachAvailability, CoachSport, TrainerCheckIn

admin.site.register(Coach)
admin.site.register(CoachSport)
admin.site.register(CoachAvailability)
admin.site.register(CoachAssignment)
admin.site.register(TrainerCheckIn)
