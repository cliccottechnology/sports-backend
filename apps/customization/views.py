from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions
from apps.common.scope import scope_queryset
from .models import (CustomizationRequest, RequestAttachment, RequestComment,
                     RequestStatusHistory)
from .serializers import (CustomizationRequestDetailSerializer,
                          CustomizationRequestListSerializer, RequestAttachmentSerializer,
                          RequestCommentSerializer)
from .utils import is_reviewer, transition_allowed

# ---------------------------------------------------------------------------
# Notification helper (in-app inbox; push only when a provider is configured)
# ---------------------------------------------------------------------------
DEDUPE_WINDOW_SECONDS = 60


def _notify(user, title, message):
    """Create one in-app notification, skipping exact duplicates fired within
    a short window (prevents double notifications on rapid retries)."""
    if not user or not getattr(user, "is_active", False):
        return
    from datetime import timedelta
    from apps.notifications.models import Notification
    from django.utils import timezone as tz
    recent = tz.now() - timedelta(seconds=DEDUPE_WINDOW_SECONDS)
    exists = Notification.objects.filter(
        user=user, title=title, message=message, channel="in_app",
        created_at__gte=recent).exists()
    if exists:
        return
    Notification.objects.create(user=user, title=title, message=message, channel="in_app")
    # Optional push: delivered only when a real provider is configured.
    try:
        from apps.notifications.push import get_push_provider
        provider = get_push_provider()
        if provider.name != "mock" and provider.is_configured():
            provider.send(title=title, body=message, user_id=user.pk)
    except Exception:
        pass


def _notify_users(users, title, message):
    for u in users:
        _notify(u, title, message)


def _super_admin_users():
    from apps.accounts.models import User
    return list(User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__name="super_admin")).distinct())


def _reviewer_users():
    """Users holding review/manage powers (a superset of super admins)."""
    from apps.accounts.models import User
    ids = set(u.pk for u in _super_admin_users())
    for u in User.objects.filter(is_active=True).exclude(role__isnull=True).select_related("role"):
        if u.has_perm_code("customization.review") or u.has_perm_code("customization.manage"):
            ids.add(u.pk)
    return User.objects.filter(pk__in=ids)


def _user_can(user, code):
    if not user or not user.is_authenticated:
        return False
    return user.has_perm_code(code)


# ---------------------------------------------------------------------------
# Action specification (permission + valid source states + required payload)
# ---------------------------------------------------------------------------
ACTION_SPEC = {
    "submit": {"perm": "customization.create", "to": "submitted", "from": ["draft"], "owner_ok": True},
    "review": {"perm": "customization.review", "to": "under_review",
               "from": ["submitted", "rejected", "clarification_required"]},
    "approve": {"perm": "customization.review", "to": "approved",
                "from": ["submitted", "under_review", "clarification_required"]},
    "reject": {"perm": "customization.review", "to": "rejected",
               "from": ["submitted", "under_review", "clarification_required", "approved"],
               "require": "reason"},
    "clarify": {"perm": "customization.review", "to": "clarification_required",
                "from": ["submitted", "under_review"], "require": "note"},
    "assign": {"perm": "customization.manage", "to": "assigned",
               "from": ["approved", "assigned", "in_progress", "ready_for_review"]},
    "start": {"perm": "customization.manage", "to": "in_progress",
              "from": ["approved", "assigned", "ready_for_review"]},
    "ready": {"perm": "customization.manage", "to": "ready_for_review", "from": ["in_progress"]},
    "complete": {"perm": "customization.review", "to": "completed",
                 "from": ["ready_for_review", "in_progress"]},
    "reopen": {"perm": "customization.manage", "to": "in_progress",
               "from": ["completed", "rejected", "cancelled"]},
    "cancel": {"perm": "customization.manage", "to": "cancelled", "owner_ok": True,
               "from": ["draft", "submitted", "under_review", "clarification_required",
                        "approved", "assigned", "in_progress", "ready_for_review"]},
    "set_priority": {"perm": "customization.review", "priority": True},
}


class CustomizationRequestViewSet(GranularPermissions, viewsets.ModelViewSet):
    perm_module = "customization"
    list_perm = "customization.view"
    retrieve_perm = "customization.view"
    create_perm = "customization.create"
    # update/destroy are gated coarsely to view; fine-grained checks happen
    # inside the methods so owners can edit drafts but not decisions.
    edit_perm = "customization.view"
    destroy_perm = "customization.manage"
    extra_action_perms = {
        "transition": "customization.view",
        "reply": "customization.reply",
        "attachments": "customization.reply",
        "stats": "customization.view",
        "assignees": "customization.view",
    }
    permission_classes = [IsAuthenticated]
    filterset_fields = ["status", "priority", "category", "school", "created_by", "assigned_to"]
    search_fields = ["title", "description", "request_id"]
    ordering_fields = ["created_at", "updated_at", "priority", "status"]
    ordering = ["-updated_at"]

    def get_serializer_class(self):
        if self.action == "list":
            return CustomizationRequestListSerializer
        return CustomizationRequestDetailSerializer

    def get_queryset(self):
        qs = CustomizationRequest.objects.select_related(
            "created_by", "school", "assigned_to").prefetch_related(
            "history__changed_by", "comments__author", "comments__attachments",
            "attachments__uploaded_by")
        user = self.request.user
        if is_reviewer(user):
            qs = scope_queryset(qs, user, "school_id")
        else:
            scope = Q(created_by=user)
            if getattr(user, "school_id", None):
                scope |= Q(school_id=user.school_id)
            qs = qs.filter(scope)

        params = self.request.query_params
        request_id = params.get("request_id")
        if request_id:
            qs = qs.filter(request_id__icontains=request_id)
        module = params.get("module_page")
        if module:
            qs = qs.filter(module_page__icontains=module)
        date_from = params.get("date_from")
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        date_to = params.get("date_to")
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)
        return qs

    # -- create / update --------------------------------------------------
    def perform_create(self, serializer):
        user = self.request.user
        data = serializer.validated_data
        as_draft = bool(self.request.data.get("save_as_draft"))
        req = serializer.save(
            created_by=user, school=getattr(user, "school", None),
            status="draft" if as_draft else "submitted",
            submitted_at=None if as_draft else timezone.now())
        self._log_history(req, "", req.status, user, "Request created.")
        log_audit(self.request, "customization.create", "customization",
                  f"request:{req.request_id}",
                  new={"title": req.title, "category": req.category, "status": req.status})
        if not as_draft:
            self._notify_submitted(req)
        return req

    def update(self, request, *args, **kwargs):
        req = self.get_object()
        user = request.user
        if not is_reviewer(user):
            if req.created_by_id != user.pk:
                raise PermissionDenied("You may only edit your own requests.")
            if req.status not in ("draft", "submitted"):
                raise PermissionDenied("This request can no longer be edited directly.")
        old = {"title": req.title, "category": req.category, "priority": req.priority,
               "description": req.description}
        response = super().update(request, *args, **kwargs)
        req.refresh_from_db()
        log_audit(request, "customization.update", "customization",
                  f"request:{req.request_id}", old=old,
                  new={"title": req.title, "category": req.category, "priority": req.priority})
        return response

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        if not is_reviewer(self.request.user) and instance.created_by_id != self.request.user.pk:
            raise PermissionDenied("Not allowed.")
        log_audit(self.request, "customization.delete", "customization",
                  f"request:{instance.request_id}", old={"title": instance.title})
        instance.delete()

    # -- helpers ----------------------------------------------------------
    def _log_history(self, req, from_status, to_status, user, note=""):
        RequestStatusHistory.objects.create(
            request=req, from_status=from_status or "", to_status=to_status,
            changed_by=user if getattr(user, "is_authenticated", False) else None, note=note)

    def _notify_submitted(self, req):
        _notify_users(_reviewer_users(), f"New customization request {req.request_id}",
                      f"{req.title} from {req.created_by.username if req.created_by else 'an admin'} "
                      f"is awaiting review.")

    # -- workflow dispatch ------------------------------------------------
    @action(detail=True, methods=["post"])
    def transition(self, request, pk=None):
        req = self.get_object()
        name = (request.data.get("action") or "").strip()
        spec = ACTION_SPEC.get(name)
        if not spec:
            return Response({"detail": "Unknown action."}, status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        if not _user_can(user, spec["perm"]):
            if not (spec.get("owner_ok") and req.created_by_id == user.pk):
                raise PermissionDenied("You do not have permission to perform this action.")

        note = (request.data.get("note") or "").strip()
        reason = (request.data.get("reason") or "").strip()
        if spec.get("require") == "reason" and not reason:
            raise ValidationError({"reason": "A reason is mandatory."})
        if spec.get("require") == "note" and not note:
            raise ValidationError({"note": "A note is required."})

        old_status = req.status

        if spec.get("priority"):
            priority = request.data.get("priority")
            if priority not in dict(CustomizationRequest.PRIORITY):
                raise ValidationError({"priority": "Invalid priority."})
            req.priority = priority
            req.save(update_fields=["priority", "updated_at"])
            RequestComment.objects.create(request=req, author=user,
                                          body=note or f"Priority set to {req.get_priority_display()}.",
                                          kind="progress")
            self._after_action(req, name)
            log_audit(request, "customization.set_priority", "customization",
                      f"request:{req.request_id}", new={"priority": priority})
            return Response(CustomizationRequestDetailSerializer(req, context={"request": request}).data)

        to_status = spec["to"]
        if req.status not in spec["from"] or not transition_allowed(req.status, to_status):
            raise ValidationError({"status": f"Cannot move from '{req.get_status_display()}' "
                                            f"to '{dict(CustomizationRequest.STATUS).get(to_status)}'."})

        req.status = to_status
        update_fields = ["status", "updated_at"]
        if to_status == "submitted" and not req.submitted_at:
            req.submitted_at = timezone.now()
            update_fields.append("submitted_at")
        if to_status == "completed":
            req.completed_at = timezone.now()
            update_fields.append("completed_at")
        else:
            req.completed_at = None
            update_fields.append("completed_at")
        if name == "reject":
            req.decision_reason = reason
            update_fields.append("decision_reason")
        if name == "assign":
            self._apply_assignment(req, request.data)
            update_fields += ["assigned_to", "assigned_team"]
        req.save(update_fields=list(dict.fromkeys(update_fields)))

        kind = "decision" if name in ("approve", "reject", "complete") else "progress"
        body = {"reject": reason, "approve": note or "Approved for implementation.",
                "clarify": note}.get(name) or note
        if body:
            RequestComment.objects.create(request=req, author=user, body=body, kind=kind)
        self._log_history(req, old_status, to_status, user, note or reason)
        self._after_action(req, name)
        log_audit(request, f"customization.{name}", "customization",
                  f"request:{req.request_id}", old={"status": old_status},
                  new={"status": to_status})
        return Response(CustomizationRequestDetailSerializer(req, context={"request": request}).data)

    def _apply_assignment(self, req, data):
        from apps.accounts.models import User
        if "assigned_to" in data:
            value = data.get("assigned_to")
            if value in (None, "", "null"):
                req.assigned_to = None
            else:
                try:
                    req.assigned_to = User.objects.get(pk=int(value))
                except (User.DoesNotExist, TypeError, ValueError):
                    raise ValidationError({"assigned_to": "Unknown user."})
        if "assigned_team" in data:
            req.assigned_team = (data.get("assigned_team") or "").strip()[:150]

    def _after_action(self, req, name):
        raiser = req.created_by
        if name == "submit":
            self._notify_submitted(req)
        elif name == "clarify":
            _notify(raiser, f"Clarification needed: {req.request_id}",
                    "The Super Admin requested more information on your customization request.")
        elif name == "approve":
            _notify(raiser, f"Request approved: {req.request_id}",
                    "Your customization request has been approved for implementation.")
        elif name == "reject":
            _notify(raiser, f"Request rejected: {req.request_id}",
                    f"Your customization request was rejected. Reason: {req.decision_reason}")
        elif name == "assign":
            _notify(raiser, f"Work assigned: {req.request_id}",
                    "Your customization request has been assigned for implementation.")
            if req.assigned_to:
                _notify(req.assigned_to, f"Assigned: {req.request_id}",
                        f"You have been assigned customization request '{req.title}'.")
        elif name == "start":
            _notify(raiser, f"Implementation started: {req.request_id}",
                    "Work on your customization request has begun.")
        elif name == "ready":
            _notify_users(_reviewer_users(), f"Ready for review: {req.request_id}",
                          f"'{req.title}' is ready for verification.")
        elif name == "complete":
            _notify(raiser, f"Request completed: {req.request_id}",
                    "Your customization request has been verified and completed.")
        elif name == "reopen":
            _notify(raiser, f"Request reopened: {req.request_id}",
                    "Your customization request has been reopened for further changes.")

    # -- comments / replies ----------------------------------------------
    @action(detail=True, methods=["post"])
    def reply(self, request, pk=None):
        req = self.get_object()
        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "A message is required."}, status=status.HTTP_400_BAD_REQUEST)
        reviewer = is_reviewer(request.user)
        is_internal = bool(request.data.get("is_internal")) and reviewer
        comment = RequestComment.objects.create(
            request=req, author=request.user, body=body,
            kind="reply" if not reviewer else "comment", is_internal=is_internal)

        # An admin reply to a clarification returns the request to review.
        if (not reviewer and req.created_by_id == request.user.pk
                and req.status == "clarification_required"):
            old = req.status
            req.status = "under_review"
            req.save(update_fields=["status", "updated_at"])
            self._log_history(req, old, "under_review", request.user, "Admin replied to clarification.")
            _notify_users(_reviewer_users(), f"Clarification reply: {req.request_id}",
                          "The admin replied to your clarification request.")

        if reviewer and not is_internal:
            _notify(req.created_by, f"Update on {req.request_id}",
                    "The Super Admin added a comment on your customization request.")

        log_audit(request, "customization.reply", "customization",
                  f"request:{req.request_id}", new={"comment": comment.pk, "internal": is_internal})
        return Response(CustomizationRequestDetailSerializer(req, context={"request": request}).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def attachments(self, request, pk=None):
        req = self.get_object()
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
        comment_id = request.data.get("comment")
        comment = None
        if comment_id:
            comment = req.comments.filter(pk=comment_id).first()
        att = RequestAttachment.objects.create(
            request=req, comment=comment, file=f, uploaded_by=request.user)
        log_audit(request, "customization.attachment", "customization",
                  f"request:{req.request_id}", new={"attachment": att.pk})
        return Response(RequestAttachmentSerializer(att).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def comments(self, request, pk=None):
        req = self.get_object()
        qs = req.comments.select_related("author").prefetch_related("attachments")
        if not is_reviewer(request.user):
            qs = qs.filter(is_internal=False)
        return Response(RequestCommentSerializer(qs, many=True, context={"request": request}).data)

    # -- lookups ----------------------------------------------------------
    @action(detail=False, methods=["get"])
    def stats(self, request):
        qs = self.get_queryset()
        counts = {key: 0 for key, _ in CustomizationRequest.STATUS}
        for row in qs.values("status"):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        counts["total"] = sum(counts.values())
        counts["new"] = counts.get("submitted", 0)
        counts["awaiting_review"] = counts.get("under_review", 0)
        counts["awaiting_clarification"] = counts.get("clarification_required", 0)
        return Response(counts)

    @action(detail=False, methods=["get"])
    def assignees(self, request):
        from apps.accounts.models import User
        from apps.common.scope import user_school_id
        qs = User.objects.filter(is_active=True).filter(
            Q(is_superuser=True) | Q(role__name__in=["super_admin", "academy_admin"])).distinct()
        sid = user_school_id(request.user)
        if sid is not None:
            qs = qs.filter(school_id=sid)
        data = [{"id": u.pk, "username": u.username,
                 "role": getattr(u.role, "name", "")} for u in qs[:200]]
        return Response(data)
