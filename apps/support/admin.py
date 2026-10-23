from django.contrib import admin
from .models import SupportTicket, TicketAttachment, TicketReply

admin.site.register(SupportTicket)
admin.site.register(TicketReply)
admin.site.register(TicketAttachment)
