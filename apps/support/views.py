from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions
from .models import SupportTicket, TicketAttachment, TicketReply


def _can_manage(user) -> bool:
    """Staff who may see every ticket and change status/assignment."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False):
        return True
    return user.has_perm_code("support.manage")


class TicketAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="uploaded_by.username", read_only=True)
    file_name = serializers.SerializerMethodField()

    class Meta:
        model = TicketAttachment
        fields = ["id", "ticket", "reply", "file", "file_name", "uploaded_by",
                  "uploaded_by_name", "created_at"]
        read_only_fields = ["ticket", "reply", "uploaded_by", "created_at"]

    def get_file_name(self, obj):
        return obj.file.name.rsplit("/", 1)[-1] if obj.file else ""


class TicketReplySerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author.username", read_only=True)
    attachments = TicketAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = TicketReply
        fields = ["id", "ticket", "author", "author_name", "body", "is_internal",
                  "attachments", "created_at"]
        read_only_fields = ["ticket", "author", "created_at"]


class SupportTicketSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.username", read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    priority_display = serializers.CharField(source="get_priority_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    replies = serializers.SerializerMethodField()
    attachments = TicketAttachmentSerializer(many=True, read_only=True)
    reply_count = serializers.SerializerMethodField()

    class Meta:
        model = SupportTicket
        fields = ["id", "subject", "description", "category", "category_display",
                  "priority", "priority_display", "status", "status_display",
                  "created_by", "created_by_name", "assigned_to", "assigned_to_name",
                  "school", "created_at", "updated_at", "resolved_at",
                  "replies", "reply_count", "attachments"]
        read_only_fields = ["created_by", "created_at", "updated_at", "resolved_at"]

    def _visible_replies(self, obj):
        qs = obj.replies.select_related("author").all()
        request = self.context.get("request")
        if request and not _can_manage(request.user):
            qs = qs.filter(is_internal=False)
        return qs

    def get_replies(self, obj):
        return TicketReplySerializer(self._visible_replies(obj), many=True,
                                     context=self.context).data

    def get_reply_count(self, obj):
        return self._visible_replies(obj).count()


class SupportTicketViewSet(GranularPermissions, viewsets.ModelViewSet):
    serializer_class = SupportTicketSerializer
    perm_module = "support"
    list_perm = "support.view"
    retrieve_perm = "support.view"
    create_perm = "support.create"
    edit_perm = "support.manage"
    destroy_perm = "support.manage"
    extra_action_perms = {
        "reply": "support.reply",
        "assign": "support.manage",
        "attachments": "support.reply",
        "stats": "support.view",
    }
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "priority", "category"]
    search_fields = ["subject", "description"]
    ordering_fields = ["updated_at", "created_at", "priority", "status"]
    ordering = ["-updated_at"]

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        qs = SupportTicket.objects.select_related(
            "created_by", "assigned_to", "school").prefetch_related(
            "replies__author", "replies__attachments", "attachments__uploaded_by")
        user = self.request.user
        if _can_manage(user):
            return scope_queryset(qs, user, "school_id")
        # Regular raisers see only tickets they opened or were assigned.
        from django.db.models import Q
        return qs.filter(Q(created_by=user) | Q(assigned_to=user))

    def perform_create(self, serializer):
        user = self.request.user
        ticket = serializer.save(created_by=user, school=getattr(user, "school", None))
        log_audit(self.request, "support.ticket.create", "support", f"ticket:{ticket.pk}",
                  new={"subject": ticket.subject, "category": ticket.category,
                       "priority": ticket.priority})

    def perform_update(self, serializer):
        old = serializer.instance.status
        ticket = serializer.save()
        new_status = ticket.status
        if new_status in ("resolved", "closed"):
            if not ticket.resolved_at:
                ticket.resolved_at = timezone.now()
                ticket.save(update_fields=["resolved_at"])
        elif new_status in ("open", "in_progress"):
            if ticket.resolved_at:
                ticket.resolved_at = None
                ticket.save(update_fields=["resolved_at"])
        log_audit(self.request, "support.ticket.update", "support", f"ticket:{ticket.pk}",
                  old={"status": old},
                  new={"status": new_status, "priority": ticket.priority,
                       "assigned_to": ticket.assigned_to_id})

    def perform_destroy(self, instance):
        log_audit(self.request, "support.ticket.delete", "support", f"ticket:{instance.pk}",
                  old={"subject": instance.subject})
        instance.delete()

    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        ticket = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "Reply body is required."}, status=status.HTTP_400_BAD_REQUEST)
        internal = bool(request.data.get("is_internal")) and _can_manage(request.user)
        reply = TicketReply.objects.create(
            ticket=ticket, author=request.user, body=body, is_internal=internal)
        # Raiser activity reopens a closed/resolved ticket.
        if not _can_manage(request.user) and ticket.status in ("resolved", "closed"):
            ticket.status = "open"
            ticket.resolved_at = None
            ticket.save(update_fields=["status", "resolved_at", "updated_at"])
        else:
            ticket.save(update_fields=["updated_at"])
        log_audit(request, "support.ticket.reply", "support", f"ticket:{ticket.pk}",
                  new={"reply": reply.pk, "internal": internal})
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        from apps.accounts.models import User
        ticket = self.get_object()
        if "assigned_to" in request.data:
            value = request.data.get("assigned_to")
            if value in (None, "", "null"):
                ticket.assigned_to = None
            else:
                try:
                    ticket.assigned_to = User.objects.get(pk=int(value))
                except (User.DoesNotExist, TypeError, ValueError):
                    raise ValidationError({"assigned_to": "Unknown user."})
        new_status = request.data.get("status")
        if new_status:
            if new_status not in dict(SupportTicket.STATUS):
                raise ValidationError({"status": "Invalid status."})
            ticket.status = new_status
            if new_status in ("resolved", "closed"):
                ticket.resolved_at = timezone.now()
            else:
                ticket.resolved_at = None
        ticket.save()
        log_audit(request, "support.ticket.assign", "support", f"ticket:{ticket.pk}",
                  new={"status": ticket.status, "assigned_to": ticket.assigned_to_id})
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def attachments(self, request, pk=None):
        ticket = self.get_object()
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "file is required."}, status=status.HTTP_400_BAD_REQUEST)
        ext = (f.name.rsplit(".", 1)[-1] if "." in f.name else "").lower()
        from django.conf import settings
        if ext not in settings.ALLOWED_UPLOAD_EXTENSIONS:
            return Response({"detail": f"File type .{ext} not allowed."},
                            status=status.HTTP_400_BAD_REQUEST)
        if f.size > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            return Response({"detail": "File too large."}, status=status.HTTP_400_BAD_REQUEST)
        reply_id = request.data.get("reply")
        reply = None
        if reply_id:
            reply = ticket.replies.filter(pk=reply_id).first()
        att = TicketAttachment.objects.create(
            ticket=ticket, reply=reply, file=f, uploaded_by=request.user)
        log_audit(request, "support.ticket.attachment", "support", f"ticket:{ticket.pk}",
                  new={"attachment": att.pk})
        return Response(TicketAttachmentSerializer(att).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"])
    def stats(self, request):
        qs = self.get_queryset()
        counts = {"total": qs.count(), "open": 0, "in_progress": 0,
                  "resolved": 0, "closed": 0}
        for row in qs.values("status"):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return Response(counts)
