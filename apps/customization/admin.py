from django.contrib import admin

from .models import CustomizationRequest, RequestAttachment, RequestComment, RequestStatusHistory

admin.site.register(CustomizationRequest)
admin.site.register(RequestStatusHistory)
admin.site.register(RequestComment)
admin.site.register(RequestAttachment)
