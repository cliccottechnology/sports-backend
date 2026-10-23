from django.contrib import admin
from .models import WhatsAppMessage, WhatsAppTemplate

admin.site.register(WhatsAppTemplate)
admin.site.register(WhatsAppMessage)
