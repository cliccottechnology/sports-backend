from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.common.permissions import GranularPermissions, HasAnyPerm, HasPerm, IsParent
from .models import Parent, StudentParent


class ParentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Parent
        fields = "__all__"


class ParentSelfSerializer(serializers.ModelSerializer):
    """Contact fields a parent may read/update on their own guardian record.

    Identity/administrative fields (relationship, created_at) stay read-only so
    self-service cannot alter academy-verified data.
    """

    class Meta:
        model = Parent
        fields = ["id", "first_name", "last_name", "relationship", "mobile",
                  "whatsapp", "email", "address", "emergency_contact",
                  "communication_preference", "created_at"]
        read_only_fields = ["id", "relationship", "created_at"]


class StudentParentSerializer(serializers.ModelSerializer):
    parent_name = serializers.SerializerMethodField()
    parent_mobile = serializers.CharField(source="parent.mobile", read_only=True)

    class Meta:
        model = StudentParent
        fields = "__all__"

    def get_parent_name(self, obj):
        return f"{obj.parent.first_name} {obj.parent.last_name or ''}".strip()


class ParentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Parent.objects.all().order_by("-created_at")
    serializer_class = ParentSerializer
    perm_module = "parents"
    search_fields = ["first_name", "last_name", "mobile", "email"]
    filterset_fields = ["relationship"]

    def get_permissions(self):
        # Self-service profile is a parent-only endpoint: any parent edits only
        # the guardian record linked to its own account. Trainer/admin tokens
        # are rejected (IsParent), not merely 404'd.
        if self.action == "me":
            return [IsParent()]
        return super().get_permissions()

    @action(detail=False, methods=["get", "patch"])
    def me(self, request):
        pid = getattr(request.user, "parent_profile_id", None)
        parent = Parent.objects.filter(pk=pid).first() if pid else None
        if not parent:
            return Response(
                {"detail": "No parent profile is linked to this account."}, status=404
            )
        if request.method.lower() == "patch":
            serializer = ParentSelfSerializer(parent, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)
        return Response(ParentSelfSerializer(parent).data)

    def get_queryset(self):
        from apps.common.scope import user_school_id
        qs = super().get_queryset()
        user = self.request.user
        # Parent-role accounts see only their own guardian record.
        if (user.is_authenticated and not user.is_superuser
                and getattr(getattr(user, "role", None), "name", None) == "parent"):
            pid = getattr(user, "parent_profile_id", None)
            return qs.filter(pk=pid) if pid else qs.none()
        sid = user_school_id(user)
        if sid is None:
            return qs
        # Own school's parents plus not-yet-linked ones (linking happens right after creation).
        from django.db.models import Q
        return qs.filter(Q(student_links__student__school_id=sid) | Q(student_links__isnull=True)).distinct()


class StudentParentViewSet(viewsets.ModelViewSet):
    queryset = StudentParent.objects.select_related("student", "parent").all()
    serializer_class = StudentParentSerializer
    module = "parents"
    permission_classes = [HasPerm("students.manage")]
    filterset_fields = ["student", "parent", "is_primary"]

    def get_permissions(self):
        # Link rows are readable by either side; writes need edit rights on either side
        # (creation flows link right after creating the parent/student).
        if self.action in ("list", "retrieve"):
            return [HasAnyPerm("students.view", "parents.view")]
        return [HasAnyPerm("students.edit", "parents.edit", "students.create", "parents.create")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "student__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        student = serializer.validated_data.get("student")
        ensure_school(self.request, getattr(student, "school_id", None), field_name="student")
        serializer.save()
