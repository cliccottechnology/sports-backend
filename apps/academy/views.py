from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from .models import (AcademyAchievement, AcademySetting, Branch, School, SessionType,
                     Sport, SportCategory, TrainingLocation)


class SchoolSerializer(serializers.ModelSerializer):
    student_count = serializers.SerializerMethodField()
    batch_count = serializers.SerializerMethodField()
    coach_count = serializers.SerializerMethodField()
    user_count = serializers.SerializerMethodField()

    class Meta:
        model = School
        fields = "__all__"

    def get_student_count(self, obj):
        return obj.students.count()

    def get_batch_count(self, obj):
        return obj.batches.count()

    def get_coach_count(self, obj):
        return obj.coaches.count()

    def get_user_count(self, obj):
        return obj.users.count()


class SchoolViewSet(GranularPermissions, viewsets.ModelViewSet):
    """Onboarded schools. Only super admins manage them (plus holders of
    schools.manage); school admins never see this module."""
    queryset = School.objects.all().order_by("name")
    serializer_class = SchoolSerializer
    perm_module = "schools"
    module = "schools"
    # Creating/editing/deleting an academy is a platform act: unlike branches,
    # it is NOT satisfiable by the per-module schools.create/edit codes a tenant
    # admin may hold, so a tenant can never onboard or alter another academy.
    create_perm = "schools.manage"
    edit_perm = "schools.manage"
    destroy_perm = "schools.manage"
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]

    def perform_create(self, serializer):
        obj = serializer.save()
        log_audit(self.request, "school.create", "schools", f"school:{obj.pk}", new={"name": obj.name})

    def perform_update(self, serializer):
        obj = self.get_object()
        old = {"name": obj.name, "is_active": obj.is_active}
        obj = serializer.save()
        log_audit(self.request, "school.update", "schools", f"school:{obj.pk}", old=old,
                  new={"name": obj.name, "is_active": obj.is_active})

    def perform_destroy(self, instance):
        from rest_framework.exceptions import ValidationError
        members = (instance.students.count() + instance.batches.count()
                   + instance.coaches.count() + instance.users.count())
        if members:
            raise ValidationError("School still has students, batches, coaches or users. Move or remove them first.")
        log_audit(self.request, "school.delete", "schools", f"school:{instance.pk}",
                  old={"name": instance.name})
        instance.delete()


class SportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Sport
        fields = "__all__"


class SportCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SportCategory
        fields = "__all__"


class TrainingLocationSerializer(serializers.ModelSerializer):
    school_name = serializers.CharField(source="school.name", read_only=True)
    branch_name = serializers.CharField(source="branch.name", read_only=True)

    class Meta:
        model = TrainingLocation
        fields = "__all__"


class BranchSerializer(serializers.ModelSerializer):
    school_name = serializers.CharField(source="school.name", read_only=True)
    location_count = serializers.SerializerMethodField()
    # Optional in the payload: a tenant administrator creating a branch inside
    # their OWN academy omits it and perform_create stamps it. Only Super
    # Admins may name a different academy, and then it is still validated.
    school = serializers.PrimaryKeyRelatedField(queryset=School.objects.all(), required=False)

    class Meta:
        model = Branch
        fields = "__all__"
        # Drop the auto UniqueTogetherValidator for ("school", "name"): it would
        # require `school` in the payload, defeating the stamping below. Uniqueness
        # is still enforced by the DB constraint, surfaced as a 400 in perform_create.
        validators = []

    def get_location_count(self, obj):
        return obj.locations.count()


class SessionTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionType
        fields = "__all__"


class SportViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Sport.objects.all().order_by("name")
    serializer_class = SportSerializer
    perm_module = "sports"
    module = "sports"
    permission_classes = [HasPerm("masters.view")]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code"]

    def perform_create(self, serializer):
        obj = serializer.save()
        log_audit(self.request, "sport.create", "masters", f"sport:{obj.pk}", new={"name": obj.name})


class SportCategoryViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = SportCategory.objects.select_related("sport").all()
    serializer_class = SportCategorySerializer
    perm_module = "sports"
    module = "sports"
    permission_classes = [HasPerm("masters.view")]
    filterset_fields = ["sport", "is_active"]


class TrainingLocationViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = TrainingLocation.objects.select_related("school", "branch").all()
    serializer_class = TrainingLocationSerializer
    perm_module = "sports"
    module = "sports"
    permission_classes = [HasPerm("masters.view")]
    filterset_fields = ["is_active", "school", "branch"]
    search_fields = ["name"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "school_id")

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        serializer.save(school_id=ensure_school(self.request, serializer.validated_data.get("school")))

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "school" in serializer.validated_data:
            ensure_school(self.request, serializer.validated_data.get("school"))
        serializer.save()


class BranchViewSet(GranularPermissions, viewsets.ModelViewSet):
    """Branches/locations inside an academy. Scoped to the caller's school."""
    queryset = Branch.objects.select_related("school").all()
    serializer_class = BranchSerializer
    perm_module = "schools"
    module = "schools"
    filterset_fields = ["is_active", "school"]
    search_fields = ["name", "code"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        return scope_queryset(super().get_queryset(), self.request.user, "school_id")

    def perform_create(self, serializer):
        from django.db import IntegrityError
        from rest_framework.exceptions import ValidationError
        from apps.common.scope import ensure_school
        school_id = ensure_school(self.request, serializer.validated_data.get("school"))
        if school_id is None:
            raise ValidationError({"school": "This field is required."})
        try:
            obj = serializer.save(school_id=school_id)
        except IntegrityError:
            raise ValidationError({"name": "This academy already has a branch with that name."})
        log_audit(self.request, "branch.create", "schools", f"branch:{obj.pk}",
                  new={"name": obj.name, "school_id": obj.school_id})

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "school" in serializer.validated_data:
            ensure_school(self.request, serializer.validated_data.get("school"))
        obj = serializer.save()
        log_audit(self.request, "branch.update", "schools", f"branch:{obj.pk}",
                  new={"name": obj.name, "is_active": obj.is_active})


class SessionTypeViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = SessionType.objects.all()
    serializer_class = SessionTypeSerializer
    perm_module = "sports"
    module = "sports"
    permission_classes = [HasPerm("masters.view")]


class AcademyAchievementSerializer(serializers.ModelSerializer):
    sport_name = serializers.CharField(source="sport.name", read_only=True)

    class Meta:
        model = AcademyAchievement
        fields = "__all__"
        read_only_fields = ["created_by", "created_at"]


class AcademyAchievementViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = AcademyAchievement.objects.select_related("sport").all()
    serializer_class = AcademyAchievementSerializer
    perm_module = "sports"
    module = "sports"
    list_perm = "sports.view"
    retrieve_perm = "sports.view"
    create_perm = "sports.create"
    edit_perm = "sports.edit"
    destroy_perm = "sports.archive"
    permission_classes = [HasPerm("masters.view")]
    filterset_fields = ["sport", "is_active"]
    search_fields = ["title", "description"]

    def perform_create(self, serializer):
        obj = serializer.save(created_by=self.request.user)
        log_audit(self.request, "academy.achievement_create", "sports", f"achievement:{obj.pk}",
                  new={"title": obj.title})


class AcademySettingSerializer(serializers.ModelSerializer):
    school_name = serializers.CharField(source="school.name", read_only=True)
    scope = serializers.ReadOnlyField()

    class Meta:
        model = AcademySetting
        fields = "__all__"


class AcademySettingViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = AcademySetting.objects.select_related("school").all().order_by("key")
    serializer_class = AcademySettingSerializer
    perm_module = "settings"
    module = "settings"
    edit_perm = "settings.edit"
    extra_action_perms = {"brand": None, "modules": None}
    filterset_fields = ["key", "school"]

    def get_permissions(self):
        # NOTE: @action-level `permission_classes` are NOT honored by DRF here —
        # this override decides everything, so the public brand action is listed.
        if self.action in ("brand", "modules"):
            return []
        if self.action in ("list", "retrieve"):
            return [HasPerm("settings.view")]
        return [HasPerm("settings.edit")]

    def get_queryset(self):
        from django.db.models import Q
        from apps.common.scope import user_school_id
        qs = super().get_queryset()
        sid = user_school_id(self.request.user)
        if sid is None:
            school = self.request.query_params.get("school")
            if school not in (None, ""):
                return qs.filter(Q(school_id=school) | Q(school__isnull=True))
            return qs
        return qs.filter(Q(school_id=sid) | Q(school__isnull=True))

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        school_id = ensure_school(self.request, serializer.validated_data.get("school"))
        from rest_framework.exceptions import ValidationError
        if AcademySetting.objects.filter(school_id=school_id, key=serializer.validated_data["key"]).exists():
            raise ValidationError({"key": "This academy already has a setting with that key."})
        obj = serializer.save(school_id=school_id)
        log_audit(self.request, "setting.create", "settings", f"setting:{obj.pk}",
                  new={"key": obj.key, "school_id": obj.school_id})

    def perform_update(self, serializer):
        from apps.common.scope import ensure_school
        if "school" in serializer.validated_data:
            ensure_school(self.request, serializer.validated_data.get("school"))
        obj = serializer.save()
        log_audit(self.request, "setting.update", "settings", f"setting:{obj.pk}",
                  new={"key": obj.key, "value": obj.value, "school_id": obj.school_id})

    def perform_destroy(self, instance):
        log_audit(self.request, "setting.delete", "settings", f"setting:{instance.pk}",
                  old={"key": instance.key, "school_id": instance.school_id})
        instance.delete()

    def perform_authentication(self, request):
        # Public endpoint: skip authentication entirely. DRF authenticates (and
        # can raise 401) *before* checking permissions, so a stale/expired token
        # attached by the client would otherwise reject this AllowAny view.
        # `self.action` is already set here (set in initialize_request, which
        # runs before initial()).
        if getattr(self, "action", None) in ("brand", "modules"):
            return
        return super().perform_authentication(request)

    def _brand_school_id(self, request):
        raw = request.query_params.get("school")
        if raw in (None, ""):
            # Only used for scoping, never for authorisation. A stale/invalid
            # token must not break this public endpoint, so swallow the
            # AuthenticationFailed that DRF raises on the lazy `request.user`.
            try:
                user = request.user
            except Exception:
                return None
            if not user or not user.is_authenticated:
                return None
            from apps.common.scope import user_school_id
            return user_school_id(user)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @action(detail=False, methods=["get"], permission_classes=[])
    def brand(self, request):
        """Public academy identity + contact details for login screens and apps.

        Authentication is intentionally NOT required here — the login page and
        coach/parent devices need this before anyone signs in. Only a fixed
        whitelist of public identity/contact keys is exposed (never the full
        settings store), so this cannot leak internal configuration. Passing
        `?school=<id>` returns that academy's overrides; otherwise the
        platform-wide defaults are returned.
        """
        keys = ["academy.name", "academy.tagline", "academy.phone",
                "academy.email", "academy.address", "academy.timings"]
        rows = list(AcademySetting.objects.filter(key__in=keys))
        sid = self._brand_school_id(request)
        stored = {s.key: s.value for s in rows if s.school_id is None}
        if sid is not None:
            stored.update({s.key: s.value for s in rows if s.school_id == sid})
        return Response({
            "name": stored.get("academy.name") or "",
            "tagline": stored.get("academy.tagline") or "",
            "phone": stored.get("academy.phone") or "",
            "email": stored.get("academy.email") or "",
            "address": stored.get("academy.address") or "",
            "timings": stored.get("academy.timings") or "",
        })

    @action(detail=False, methods=["get"], permission_classes=[])
    def modules(self, request):
        """Public module availability for the caller's (or `?school=`) academy.

        Lets the login screen / apps hide modules that are switched off, while
        the API itself enforces the same gate per request.
        """
        from apps.common.modules import MODULE_CODES, disabled_modules
        sid = self._brand_school_id(request)
        off = disabled_modules(sid)
        return Response({
            "school": sid,
            "modules": [{"module": m, "enabled": m not in off} for m in MODULE_CODES],
        })
