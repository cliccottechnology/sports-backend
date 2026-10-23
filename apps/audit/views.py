from rest_framework import generics, serializers
from apps.common.permissions import HasPerm
from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = AuditLog
        fields = "__all__"


class AuditLogListView(generics.ListAPIView):
    queryset = AuditLog.objects.select_related("user").all()
    serializer_class = AuditLogSerializer
    permission_classes = [HasPerm("audit.view")]
    filterset_fields = ["action", "module", "user"]
    search_fields = ["record", "action", "module"]
    ordering_fields = ["created_at"]
