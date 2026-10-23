from django.contrib import admin
from .models import AttendanceCorrection, AttendancePhotoEvidence, AttendanceRecord

admin.site.register(AttendanceRecord)
admin.site.register(AttendanceCorrection)
admin.site.register(AttendancePhotoEvidence)
