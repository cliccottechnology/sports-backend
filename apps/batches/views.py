from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import GranularPermissions, HasAnyPerm, HasPerm
from .models import Batch, BatchStudent


class BatchSerializer(serializers.ModelSerializer):
    student_count = serializers.SerializerMethodField()
    sport_name = serializers.CharField(source="sport.name", read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    coach_name = serializers.SerializerMethodField()
    students = serializers.SerializerMethodField()

    class Meta:
        model = Batch
        fields = "__all__"

    def get_student_count(self, obj):
        return obj.rosters.filter(is_active=True).count()

    def get_students(self, obj):
        links = obj.rosters.filter(is_active=True).select_related("student__sport")
        return [
            {
                "id": link.student_id,
                "student_id": link.student.student_id,
                "name": f"{link.student.first_name} {link.student.last_name}".strip(),
                "first_name": link.student.first_name,
                "last_name": link.student.last_name,
                "sport": link.student.sport.name if link.student.sport else (obj.sport.name if obj.sport else "—"),
                "phone": link.student.phone or "—",
                "status": link.student.status,
                "joined_on": str(link.joined_on),
            }
            for link in links
        ]

    def get_coach_name(self, obj):
        if obj.coach:
            return f"{obj.coach.first_name} {obj.coach.last_name}".strip()
        return ""

    def validate_training_days(self, value):
        # Manual selection: distinct weekdays 0 (Mon)..6 (Sun), at least one.
        # Empty = unset (preserves older records).
        if value in (None, []):
            return value
        if not isinstance(value, list) or len(set(value)) < 1:
            raise serializers.ValidationError("Pick at least one training day.")
        if any(not isinstance(d, int) or d < 0 or d > 6 for d in value):
            raise serializers.ValidationError("Training days must be weekdays 0 (Mon) to 6 (Sun).")
        return sorted(set(value))


class BatchStudentSerializer(serializers.ModelSerializer):
    student_name = serializers.SerializerMethodField()
    student_id = serializers.CharField(source="student.student_id", read_only=True)
    student_sport = serializers.CharField(source="student.sport.name", read_only=True, default="—")
    student_phone = serializers.CharField(source="student.phone", read_only=True, default="—")
    student_status = serializers.CharField(source="student.status", read_only=True, default="active")
    batch_name = serializers.CharField(source="batch.name", read_only=True)
    batch_sport = serializers.CharField(source="batch.sport.name", read_only=True)

    class Meta:
        model = BatchStudent
        fields = "__all__"

    def get_student_name(self, obj):
        if obj.student:
            return f"{obj.student.first_name} {obj.student.last_name}".strip()
        return "" 


class BatchViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Batch.objects.select_related("sport", "coach", "location", "school").prefetch_related("rosters__student__sport").all().order_by("name")
    serializer_class = BatchSerializer
    perm_module = "batches"
    filterset_fields = ["sport", "coach", "is_active", "location", "school"]
    search_fields = ["name", "sport__name", "rosters__student__first_name", "rosters__student__last_name", "rosters__student__student_id"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        try:
            given = serializer.validated_data.pop("school", None)
            serializer.save(school_id=ensure_school(self.request, given))
        except DjangoValidationError as e:
            raise serializers.ValidationError(str(e))


class BatchStudentViewSet(viewsets.ModelViewSet):
    queryset = BatchStudent.objects.select_related("batch__sport", "student__sport").all().order_by("-joined_on", "-id")
    serializer_class = BatchStudentSerializer
    module = "batches"
    permission_classes = [HasPerm("batches.manage")]
    filterset_fields = ["batch", "student", "is_active"]
    search_fields = ["batch__name", "student__student_id", "student__first_name", "student__last_name", "student__sport__name"]

    def get_permissions(self):
        # Roster membership is readable with either side's view right; assigning
        # students happens inside student/batch management flows (incl. creation).
        if self.action in ("list", "retrieve"):
            return [HasAnyPerm("batches.view", "students.view")]
        return [HasAnyPerm("batches.edit", "students.edit", "students.create")]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "batch__school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        batch = serializer.validated_data.get("batch")
        student = serializer.validated_data.get("student")
        ensure_school(self.request, getattr(batch, "school_id", None), field_name="batch")
        ensure_school(self.request, getattr(student, "school_id", None), field_name="student")
        bs, ss = getattr(batch, "school_id", None), getattr(student, "school_id", None)
        if bs and ss and bs != ss:
            raise serializers.ValidationError("Batch and student belong to different schools.")
        try:
            serializer.save()
        except DjangoValidationError as e:
            raise serializers.ValidationError(str(e))
