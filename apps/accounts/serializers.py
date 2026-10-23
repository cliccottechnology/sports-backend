from rest_framework import serializers
from .models import Permission, Role, User


class PermissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "code", "name", "module"]


def _check_password_policy(pwd: str, username: str = ""):
    from rest_framework import serializers as _s
    if not pwd or len(pwd) < 8:
        raise _s.ValidationError("Password must be at least 8 characters.")
    if username and pwd.lower() == username.lower():
        raise _s.ValidationError("Password must not be the same as the username.")


class RoleSerializer(serializers.ModelSerializer):
    permissions = PermissionSerializer(many=True, read_only=True)
    permission_codes = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    member_count = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = ["id", "name", "description", "permissions", "permission_codes", "member_count"]

    def get_member_count(self, obj):
        return obj.users.count()

    def validate_permission_codes(self, codes):
        known = set(Permission.objects.values_list("code", flat=True))
        unknown = [c for c in codes if c not in known]
        if unknown:
            raise serializers.ValidationError(f"Unknown permission codes: {', '.join(unknown)}")
        return codes

    def create(self, validated):
        codes = validated.pop("permission_codes", [])
        role = super().create(validated)
        if codes:
            perms = Permission.objects.filter(code__in=codes)
            role.permissions.set(perms)
        return role

    def update(self, instance, validated):
        codes = validated.pop("permission_codes", None)
        role = super().update(instance, validated)
        if codes is not None:
            role.permissions.set(Permission.objects.filter(code__in=codes))
        return role


class UserSerializer(serializers.ModelSerializer):
    role_name = serializers.SerializerMethodField()
    school_name = serializers.CharField(source="school.name", read_only=True)
    permissions = serializers.SerializerMethodField()
    # The guardian record linked to this account (parent logins only). Lets the
    # Parent App confirm its identity without ever trusting a client-sent id.
    parent_id = serializers.SerializerMethodField()
    # Optional on update: blank/absent = keep the current password.
    password = serializers.CharField(write_only=True, required=False, min_length=8)

    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name", "phone",
                  "role", "role_name", "school", "school_name", "permissions", "is_active", "is_archived",
                  "is_superuser", "must_change_password", "password", "parent_id",
                  "date_joined", "last_login"]
        read_only_fields = ["date_joined", "last_login", "must_change_password"]

    def get_role_name(self, obj):
        return obj.role.name if obj.role else None

    def get_permissions(self, obj):
        return obj.perm_codes()

    def get_parent_id(self, obj):
        return getattr(obj, "parent_profile_id", None)

    def update(self, instance, validated):
        pwd = validated.pop("password", None)
        if pwd:
            _check_password_policy(pwd, validated.get("username", instance.username))
        user = super().update(instance, validated)
        if pwd:
            user.set_password(pwd)
            user.save(update_fields=["password"])
        return user


class UserCreateSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["username", "email", "password", "first_name", "last_name", "phone", "role", "school", "is_active"]

    def validate(self, attrs):
        _check_password_policy(attrs.get("password", ""), attrs.get("username", ""))
        return attrs

    def create(self, validated):
        pwd = validated.pop("password")
        user = User(**validated)
        user.set_password(pwd)
        user.save()
        return user
