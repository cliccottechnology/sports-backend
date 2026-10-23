"""Super Admin module enablement control surface.

One screen lists every academy against every platform module; switching one off
denies that academy's users on the very next API call (see
apps/common/modules.py). Writes are platform-level, so they require
schools.manage — academy admins may read their own row but never widen access.
"""
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.common.modules import module_label
from apps.accounts.rbac_catalog import MODULE_ORDER
from apps.academy.models import AcademyModule, School
from apps.audit.utils import log_audit
from apps.common.permissions import HasPerm


class AcademyModuleSerializer(serializers.ModelSerializer):
    school_name = serializers.CharField(source="school.name", read_only=True)

    class Meta:
        model = AcademyModule
        fields = "__all__"


class AcademyModuleViewSet(viewsets.ModelViewSet):
    """Per-academy module switches (Super Admin)."""
    queryset = AcademyModule.objects.select_related("school").all().order_by("school__name", "module")
    serializer_class = AcademyModuleSerializer
    permission_classes = [HasPerm("schools.manage")]
    filterset_fields = ["school", "module", "enabled"]

    def get_queryset(self):
        from apps.common.scope import user_school_id
        qs = super().get_queryset()
        sid = user_school_id(self.request.user)
        if sid is None:
            return qs
        return qs.filter(school_id=sid)

    def _validate(self, data):
        module = data.get("module")
        if module not in MODULE_ORDER:
            raise ValidationError({"module": "Unknown module."})
        school = data.get("school")
        if not School.objects.filter(pk=getattr(school, "pk", school)).exists():
            raise ValidationError({"school": "Unknown academy."})

    def perform_create(self, serializer):
        self._validate(serializer.validated_data)
        obj = serializer.save()
        log_audit(self.request, "module.enable", "schools", f"module:{obj.school_id}:{obj.module}",
                  new={"module": obj.module, "enabled": obj.enabled, "config": obj.config})

    def perform_update(self, serializer):
        self._validate(serializer.validated_data)
        obj = serializer.save()
        log_audit(self.request, "module.update", "schools", f"module:{obj.school_id}:{obj.module}",
                  new={"module": obj.module, "enabled": obj.enabled, "config": obj.config})

    def perform_destroy(self, instance):
        log_audit(self.request, "module.reset", "schools",
                  f"module:{instance.school_id}:{instance.module}",
                  old={"module": instance.module, "enabled": instance.enabled})
        instance.delete()

    @action(detail=False, methods=["get"])
    def catalog(self, request):
        """Every platform module, so the Super Admin screen can render rows."""
        return Response([{"module": m, "label": module_label(m)} for m in MODULE_ORDER])

    @action(detail=False, methods=["get", "put"], url_path="matrix")
    def matrix(self, request):
        """Academy x module grid. PUT upserts a single cell.

        A missing row means "enabled" (backward compatible with academies that
        predate this feature).
        """
        if request.method == "PUT":
            data = dict(request.data or {})
            if not data.get("school"):
                raise ValidationError({"school": "Required."})
            self._validate(data)
            obj, _created = AcademyModule.objects.update_or_create(
                school_id=int(data["school"]), module=data["module"],
                defaults={"enabled": bool(data.get("enabled", True)),
                          "config": data.get("config") or {}})
            log_audit(self.request, "module.update", "schools",
                      f"module:{obj.school_id}:{obj.module}",
                      new={"module": obj.module, "enabled": obj.enabled, "config": obj.config})
            return Response(AcademyModuleSerializer(obj).data)

        flags = {(f.school_id, f.module): f for f in AcademyModule.objects.all()}
        schools = School.objects.all().order_by("name")
        rows = []
        for school in schools:
            modules = []
            for code in MODULE_ORDER:
                flag = flags.get((school.pk, code))
                modules.append({
                    "module": code,
                    "label": module_label(code),
                    "enabled": True if flag is None else bool(flag.enabled),
                    "configured": flag is not None,
                    "config": (flag.config if flag else {}) or {},
                })
            rows.append({"school": school.pk, "school_name": school.name,
                         "is_active": school.is_active, "modules": modules})
        return Response({"modules": [{"module": m, "label": module_label(m)} for m in MODULE_ORDER],
                         "rows": rows})
