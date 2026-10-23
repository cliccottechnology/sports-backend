from django.contrib import admin
from .models import AcademySetting, SessionType, Sport, SportCategory, TrainingLocation

for m in (Sport, SportCategory, TrainingLocation, SessionType, AcademySetting):
    admin.site.register(m)
