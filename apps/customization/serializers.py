from rest_framework import serializers

from .models import CustomizationRequest, RequestAttachment, RequestComment, RequestStatusHistory
from .utils import is_reviewer


class RequestAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.username", read_only=True)
    file_name = serializers.SerializerMethodField()

    class Meta:
        model = RequestAttachment
        fields = ["id", "request", "comment", "file", "file_name", "uploaded_by",
                  "uploaded_by_name", "created_at"]
        read_only_fields = ["request", "comment", "uploaded_by", "created_at"]

    def get_file_name(self, obj):
        return obj.file.name.rsplit("/", 1)[-1] if obj.file else ""


class RequestCommentSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.username", read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)
    attachments = RequestAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = RequestComment
        fields = ["id", "request", "author", "author_name", "body", "kind", "kind_display",
                  "is_internal", "attachments", "created_at"]
        read_only_fields = ["request", "author", "created_at"]


class RequestStatusHistorySerializer(serializers.ModelSerializer):
    changed_by_name = serializers.CharField(source="changed_by.username", read_only=True)

    class Meta:
        model = RequestStatusHistory
        fields = ["id", "from_status", "to_status", "changed_by", "changed_by_name", "note", "created_at"]


class CustomizationRequestListSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.username", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    comment_count = serializers.IntegerField(source="comments.count", read_only=True)
    attachment_count = serializers.IntegerField(source="attachments.count", read_only=True)

    class Meta:
        model = CustomizationRequest
        fields = ["id", "request_id", "title", "category", "category_display",
                  "priority", "priority_display", "status", "status_display",
                  "module_page", "created_by", "created_by_name", "school", "school_name",
                  "assigned_to", "assigned_to_name", "assigned_team",
                  "created_at", "updated_at", "submitted_at", "completed_at",
                  "comment_count", "attachment_count"]


class CustomizationRequestDetailSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.username", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    comments = serializers.SerializerMethodField()
    history = RequestStatusHistorySerializer(many=True, read_only=True)
    attachments = serializers.SerializerMethodField()

    class Meta:
        model = CustomizationRequest
        fields = ["id", "request_id", "title", "category", "category_display",
                  "description", "reason", "expected_outcome", "module_page",
                  "priority", "priority_display", "status", "status_display",
                  "created_by", "created_by_name", "school", "school_name",
                  "assigned_to", "assigned_to_name", "assigned_team",
                  "decision_reason", "created_at", "updated_at", "submitted_at",
                  "completed_at", "comments", "history", "attachments"]
        # Status/assignment/decision fields are workflow-controlled: only the
        # `transition` action may change them, never a direct PATCH.
        read_only_fields = ["request_id", "status", "created_by", "assigned_to",
                            "assigned_team", "created_at", "updated_at",
                            "submitted_at", "completed_at", "decision_reason"]

    def _visible_comments(self, obj):
        qs = obj.comments.select_related("author").prefetch_related("attachments").all()
        request = self.context.get("request")
        if request and not is_reviewer(request.user):
            qs = qs.filter(is_internal=False)
        return qs

    def get_comments(self, obj):
        return RequestCommentSerializer(self._visible_comments(obj), many=True,
                                        context=self.context).data

    def get_attachments(self, obj):
        # Root-level attachments (those not attached to a comment).
        qs = obj.attachments.filter(comment__isnull=True).select_related("uploaded_by")
        return RequestAttachmentSerializer(qs, many=True, context=self.context).data
