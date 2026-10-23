from django.contrib import admin
from .models import Permission, Role, RolePermission, User

admin.site.register(Permission)
admin.site.register(Role)
admin.site.register(RolePermission)
admin.site.register(User)
