from rest_framework import serializers, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.common.permissions import GranularPermissions, HasPerm
from .models import Document


class DocumentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.username", read_only=True)

    class Meta:
        model = Document
        fields = "__all__"
        read_only_fields = ["uploaded_by", "uploaded_at", "file_type"]

    def validate_file(self, f):
        from django.conf import settings
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower()
        if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
            raise serializers.ValidationError(f"File type .{ext} not allowed.")
        if f.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            raise serializers.ValidationError("File too large.")
        return f


class DocumentViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = Document.objects.select_related("uploaded_by").all().order_by("-uploaded_at")
    serializer_class = DocumentSerializer
    perm_module = "documents"
    filterset_fields = ["category"]
    search_fields = ["title"]
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        f = serializer.validated_data.get("file")
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower() if f else ""
        serializer.save(uploaded_by=self.request.user, file_type=ext)

    def perform_update(self, serializer):
        # Keep file_type in sync when the file itself is replaced.
        if "file" in serializer.validated_data:
            f = serializer.validated_data["file"]
            ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower() if f else ""
            serializer.save(file_type=ext)
        else:
            serializer.save()
