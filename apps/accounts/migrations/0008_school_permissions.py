"""Grant the schools permission rows (super admin only)."""
from django.db import migrations


def grant(apps, schema_editor):
    from apps.accounts.rbac_catalog import ALL_CODES
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    for code, name, module in ALL_CODES:
        if module == "schools":
            Permission.objects.get_or_create(code=code, defaults={"name": name, "module": module})
    try:
        super_admin = Role.objects.get(name="super_admin")
    except Role.DoesNotExist:
        return
    super_admin.permissions.add(*Permission.objects.filter(module="schools"))


def reverse_grant(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_user_school")]

    operations = [migrations.RunPython(grant, reverse_grant)]
