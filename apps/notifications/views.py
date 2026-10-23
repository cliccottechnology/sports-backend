from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.common.permissions import GranularPermissions, HasPerm
from apps.audit.utils import log_audit
from .models import Notification, NotificationTemplate


class NotificationTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationTemplate
        fields = "__all__"


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = "__all__"
        read_only_fields = ["created_at"]


class NotificationViewSet(GranularPermissions, viewsets.ModelViewSet):
    serializer_class = NotificationSerializer
    perm_module = "notifications"
    list_perm = "notifications.view"
    retrieve_perm = "notifications.view"
    create_perm = "notifications.send"
    edit_perm = "notifications.send"
    destroy_perm = "notifications.send"
    extra_action_perms = {"read": "notifications.view", "mark_all_read": "notifications.view",
                          "broadcast": "notifications.send", "push_status": "notifications.view"}
    permission_classes = [IsAuthenticated]
    filterset_fields = ["is_read"]

    def get_permissions(self):
        # Marking your OWN notifications read is self-service for any signed-in
        # user; everything else follows the matrix.
        if self.action in ("read", "mark_all_read"):
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_queryset(self):
        from apps.common.scope import scope_queryset
        user = self.request.user
        if user.is_superuser or (getattr(user, "role", None) and user.role.name in ("super_admin", "academy_admin")):
            qs = scope_queryset(Notification.objects.all(), user, "user__school_id")
            uid = self.request.query_params.get("user")
            if uid:
                qs = qs.filter(user_id=uid)
            return qs.order_by("-created_at")
        return Notification.objects.filter(user=user).order_by("-created_at")

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        n = self.get_object()
        n.is_read = True
        n.save(update_fields=["is_read"])
        return Response({"detail": "Marked read."})

    @action(detail=False, methods=["post"], permission_classes=[HasPerm("notifications.send")])
    def broadcast(self, request):
        """Push an announcement: in-app inbox for chosen roles + optional
        WhatsApp to targeted parents + optional push to devices. Audited
        once per run.

        Targeting (WhatsApp side): session > batch > sport > student_ids > all
        parents. Only parents whose communication_preference allows WhatsApp
        are messaged; everyone else is counted as skipped, never exposed to
        other recipients (one message row per parent).

        Channels (default ["in_app"]): in_app (always local, no provider
        needed), whatsapp (requires the whatsapp flag / provider), push
        (delivered only when a push provider is configured — otherwise
        counted as unconfigured and skipped, never silently dropped).
        """
        from apps.accounts.models import User
        from apps.parents.models import Parent, StudentParent
        from apps.students.models import Student
        from apps.whatsapp.models import WhatsAppMessage
        from apps.whatsapp.views import _deliver
        from apps.common.scope import scope_queryset
        from .push import get_push_provider
        title = (request.data.get("title") or "").strip()
        message = (request.data.get("message") or "").strip()
        if not title or not message:
            return Response({"detail": "title and message are required."}, status=400)
        if "channels" in request.data:
            channels = request.data.get("channels") or []
            if isinstance(channels, str):
                channels = [channels]
        else:
            # Legacy clients: a broadcast always pushed to in-app
            # inboxes; the whatsapp boolean added WhatsApp on top.
            channels = ["in_app"]
            if request.data.get("whatsapp"):
                channels.append("whatsapp")
        channels = [c for c in channels if c in ("in_app", "whatsapp", "push")]
        if not channels:
            return Response({"detail": "At least one channel (in_app, whatsapp, push) is required."}, status=400)
        roles = request.data.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        users = scope_queryset(User.objects.filter(is_active=True), request.user, "school_id")
        if roles:
            users = users.filter(role__name__in=roles)
        users = list(users[:500])
        inbox_count = 0
        if "in_app" in channels:
            Notification.objects.bulk_create([
                Notification(user=u, title=title, message=message, channel="in_app") for u in users
            ])
            inbox_count = len(users)
        # Push: one push notification per targeted user, delivered through
        # the configured provider. Unconfigured providers are counted,
        # never claimed as delivered.
        push_sent, push_failed, push_unconfigured, push_skipped = 0, 0, 0, 0
        if "push" in channels:
            provider = get_push_provider()
            configured = False
            try:
                configured = provider.is_configured()
            except Exception:
                configured = False
            if not configured:
                push_unconfigured = len(users)
            else:
                for u in users:
                    try:
                        result = provider.send(title=title, body=message, user_id=u.pk)
                        if result.status == "sent":
                            push_sent += 1
                        elif result.status == "skipped":
                            push_skipped += 1
                        else:
                            push_failed += 1
                    except Exception:
                        push_failed += 1
        wa_sent, wa_failed, wa_skipped = 0, 0, 0
        if "whatsapp" in channels:
            student_ids = request.data.get("student_ids") or []
            session_id = request.data.get("session")
            batch_id = request.data.get("batch")
            sport_id = request.data.get("sport")
            students = scope_queryset(Student.objects.all(), request.user, "school_id")
            if student_ids:
                students = students.filter(pk__in=[int(x) for x in student_ids if str(x).isdigit()])
            elif session_id:
                students = students.filter(sessionstudent__session_id=session_id)
            elif batch_id:
                students = students.filter(batch_links__batch_id=batch_id, batch_links__is_active=True)
            elif sport_id:
                students = students.filter(sport_id=sport_id)
            links = (StudentParent.objects.filter(student__in=students)
                     .select_related("parent", "student"))
            seen = set()
            for link in links[:500]:
                p = link.parent
                pref = (p.communication_preference or "whatsapp").lower()
                if pref not in ("whatsapp", ""):
                    wa_skipped += 1
                    continue
                num = "".join(ch for ch in str(p.mobile or p.whatsapp or "") if ch.isdigit())[-10:]
                if not num or num in seen:
                    wa_skipped += 1
                    continue
                seen.add(num)
                body = f"{title}\n{message}"
                try:
                    provider_name, result = _deliver(to_number=num, body=body)
                    WhatsAppMessage.objects.create(
                        to_number=num, body=body, related_student=link.student,
                        provider=provider_name or "mock",
                        provider_message_id=(getattr(result, "provider_message_id", "") or ""),
                        status=(getattr(result, "status", "") or "queued"),
                        failure_info=(getattr(result, "failure_info", "") or ""),
                        created_by=request.user)
                    wa_sent += 1
                except Exception as exc:
                    WhatsAppMessage.objects.create(
                        to_number=num, body=body, related_student=link.student,
                        status="failed", failure_info=str(exc)[:1000], created_by=request.user)
                    wa_failed += 1
        log_audit(request, "notifications.broadcast", "notifications", "broadcast",
                  new={"title": title, "channels": channels, "inbox": inbox_count,
                       "wa_sent": wa_sent, "push_sent": push_sent})
        return Response({"inbox": inbox_count, "wa_sent": wa_sent,
                         "wa_failed": wa_failed, "wa_skipped": wa_skipped,
                         "push_sent": push_sent, "push_failed": push_failed,
                         "push_unconfigured": push_unconfigured,
                         "push_skipped": push_skipped, "channels": channels})

    @action(detail=False, methods=["get"])
    def push_status(self, request):
        """Which push provider is live — no secrets exposed."""
        from .push import get_push_provider
        provider = get_push_provider()
        try:
            configured = provider.is_configured()
        except Exception:
            configured = False
        return Response({"provider": provider.name,
                         "configured": configured,
                         "live": provider.name != "mock" and configured})

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        """Mark the caller's visible unread notifications as read."""
        n = self.get_queryset().filter(is_read=False).update(is_read=True)
        return Response({"marked": n})


class NotificationTemplateViewSet(viewsets.ModelViewSet):
    queryset = NotificationTemplate.objects.all()
    serializer_class = NotificationTemplateSerializer
    module = "notifications"
    permission_classes = [HasPerm("settings.manage")]
