from django.contrib import admin
from .models import EnrollmentHistory, Student, StudentDocument, StudentReferencePhoto

admin.site.register(Student)
admin.site.register(StudentDocument)
admin.site.register(EnrollmentHistory)
admin.site.register(StudentReferencePhoto)
