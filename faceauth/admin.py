from django.contrib import admin

from .models import StudentFaceTemplate


@admin.register(StudentFaceTemplate)
class StudentFaceTemplateAdmin(admin.ModelAdmin):
    list_display = ("student", "school", "provider", "consent_given", "created_at")
    list_filter = ("provider", "consent_given")
    search_fields = ("student__student_id", "student__first_name", "face_id")
    readonly_fields = ("face_id", "created_at")
