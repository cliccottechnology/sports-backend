from rest_framework import serializers, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from apps.audit.utils import log_audit
from apps.common.permissions import GranularPermissions, HasPerm
from .models import WhatsAppMessage, WhatsAppTemplate
from .provider import get_whatsapp_provider


class WhatsAppTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppTemplate
        fields = "__all__"


class WhatsAppMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppMessage
        fields = "__all__"
        read_only_fields = ["provider", "provider_message_id", "status", "failure_info", "created_by", "created_at", "updated_at"]


def _render(template_body: str, variables: dict) -> str:
    out = template_body or ""
    for k, v in (variables or {}).items():
        out = out.replace("{{" + str(k) + "}}", str(v))
    return out


def _ordered_params(template_body: str, variables: dict) -> list:
    """Positional {{1}}, {{2}}… params for the Meta template API.

    Falls back to the variables' values in order when the body uses named
    {{name}} placeholders only.
    """
    import re
    slots = sorted({int(n) for n in re.findall(r"\{\{(\d+)\}\}", template_body or "")})
    if slots:
        return [str((variables or {}).get(str(i), "")) for i in slots]
    return [str(v) for v in (variables or {}).values()]


def _deliver(*, to_number: str, body: str, template=None, variables: dict | None = None):
    """Send through the configured provider. Returns (provider_name, SendResult)."""
    provider = get_whatsapp_provider()
    kwargs: dict = {}
    if template and getattr(template, "name", ""):
        kwargs = {"template_name": template.name,
                  "language": getattr(template, "language", "en") or "en",
                  "template_params": _ordered_params(getattr(template, "body", ""), variables or {})}
    return provider.name, provider.send(to_number=to_number, body=body, **kwargs)


class WhatsAppTemplateViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = WhatsAppTemplate.objects.all().order_by("name")
    serializer_class = WhatsAppTemplateSerializer
    perm_module = "whatsapp"
    list_perm = "whatsapp.view"
    retrieve_perm = "whatsapp.view"
    create_perm = "whatsapp.manage"
    edit_perm = "whatsapp.manage"
    destroy_perm = "whatsapp.manage"
    permission_classes = [HasPerm("settings.manage")]
    filterset_fields = ["category", "is_active"]


class WhatsAppMessageViewSet(GranularPermissions, viewsets.ModelViewSet):
    queryset = WhatsAppMessage.objects.select_related("template").all().order_by("-created_at")
    serializer_class = WhatsAppMessageSerializer
    perm_module = "whatsapp"
    list_perm = "whatsapp.view"
    retrieve_perm = "whatsapp.view"
    create_perm = "whatsapp.send"
    edit_perm = "whatsapp.manage"
    destroy_perm = "whatsapp.manage"
    extra_action_perms = {"retry": "whatsapp.send"}
    permission_classes = [HasPerm("notifications.send")]
    filterset_fields = ["status", "template", "related_student"]
    search_fields = ["to_number"]

    def get_queryset(self):
        # Own school's messages plus unlinked (academy-global) ones.
        from django.db.models import Q
        from apps.common.scope import user_school_id
        qs = super().get_queryset()
        sid = user_school_id(self.request.user)
        if sid is None:
            return qs
        return qs.filter(Q(related_student__school_id=sid) | Q(related_student__isnull=True))

    def create(self, request, *args, **kwargs):
        """Pre-render template → body so `body` validates even when the
        client only sends template + variables. Accepts JSON and multipart."""
        import json
        raw = request.data
        data = raw.copy() if hasattr(raw, "copy") else dict(raw)
        body = data.get("body", "")
        if isinstance(body, list):
            body = body[0] if body else ""
        if data.get("template") and not (body or "").strip():
            variables = data.get("variables", {}) or {}
            if isinstance(variables, str):
                try:
                    variables = json.loads(variables)
                except ValueError:
                    variables = {}
            tpl = WhatsAppTemplate.objects.filter(pk=data.get("template")).first()
            if tpl:
                data["body"] = _render(tpl.body, variables)
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(serializer.data, status=201, headers=self.get_success_headers(serializer.data))

    def perform_create(self, serializer):
        from apps.common.scope import ensure_school
        template = serializer.validated_data.get("template")
        related = serializer.validated_data.get("related_student")
        if related is not None:
            ensure_school(self.request, getattr(related, "school_id", None), field_name="related_student")
        variables = self.request.data.get("variables", {}) or {}
        body = _render(template.body if template else "", variables) or serializer.validated_data.get("body", "")
        if not body.strip():
            raise serializers.ValidationError("Message body is empty. Pick a template (and fill its variables) or type a body.")
        provider_name, result = _deliver(to_number=serializer.validated_data.get("to_number", ""),
                                        body=body, template=template, variables=variables)
        msg = serializer.save(provider=provider_name, body=body, created_by=self.request.user)
        try:
            msg.provider_message_id = result.provider_message_id
            msg.status = result.status
            msg.failure_info = result.failure_info
        except Exception as exc:  # retry/error handling: record failure, never crash
            msg.status = "failed"
            msg.failure_info = str(exc)[:1000]
        msg.save()
        log_audit(self.request, "whatsapp.send", "whatsapp", f"wa:{msg.pk}", new={"to": msg.to_number, "status": msg.status})

    @action(detail=True, methods=["post"], permission_classes=[HasPerm("notifications.send")])
    def retry(self, request, pk=None):
        msg = self.get_object()
        provider_name, result = _deliver(to_number=msg.to_number, body=msg.body, template=msg.template)
        try:
            msg.provider_message_id = result.provider_message_id or msg.provider_message_id
            msg.provider = provider_name
            msg.status = result.status
            msg.failure_info = result.failure_info
        except Exception as exc:
            msg.status = "failed"
            msg.failure_info = str(exc)[:1000]
        msg.save()
        return Response(WhatsAppMessageSerializer(msg).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def provider_status(request):
    """Which provider is live — no secrets exposed. The admin banner uses this."""
    from .provider import get_whatsapp_provider
    provider = get_whatsapp_provider()
    active_templates = WhatsAppTemplate.objects.filter(is_active=True).count()
    try:
        configured = provider.is_configured()
    except Exception:
        configured = False
    return Response({"provider": provider.name, "live": provider.name != "mock" and configured,
                     "configured": configured, "active_templates": active_templates})


@api_view(["GET", "POST"])
@permission_classes([AllowAny])  # Meta calls this directly; GET verifies via token, POST is status-only
def delivery_webhook(request):
    """Delivery-status callback. Never fakes delivery.

    GET: Meta webhook verification handshake (?hub.mode=subscribe &
    hub.verify_token=<WHATSAPP_VERIFY_TOKEN> → returns hub.challenge).
    POST: accepts Meta's statuses format AND the legacy flat format
    {provider_message_id, status}.
    """
    import os
    if request.method == "GET":
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge", "")
        expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        if mode == "subscribe" and expected and token == expected:
            from django.http import HttpResponse
            return HttpResponse(challenge, content_type="text/plain")
        return Response({"detail": "Verification failed."}, status=403)

    data = request.data if isinstance(request.data, dict) else {}
    updates: list[tuple[str, str, str]] = []  # (provider_message_id, status, failure_info)
    # Meta Cloud format: {entry: [{changes: [{value: {statuses: [{id, status, errors}]}}]}]}
    for entry in data.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            for st in ((change.get("value") or {}).get("statuses", []) or []):
                mid = st.get("id", "")
                status = st.get("status", "")
                errs = st.get("errors") or []
                fail = "; ".join(str(e.get("title") or e.get("message") or e) for e in errs)[:1000]
                if mid:
                    updates.append((mid, status, fail))
    # Legacy flat format.
    if not updates:
        mid = data.get("provider_message_id") or data.get("message_id", "")
        if mid:
            updates.append((mid, data.get("status", ""), str(data.get("failure_info", ""))[:1000]))
    # Meta can also POST inbound messages / other events with no statuses.
    if not updates:
        return Response({"detail": "No status updates in payload."})
    touched = 0
    for mid, status, fail in updates:
        if status not in ("sent", "delivered", "read", "failed"):
            continue
        updated = WhatsAppMessage.objects.filter(provider_message_id=mid).update(
            status=status, failure_info=fail)
        touched += updated
    return Response({"detail": "Updated.", "touched": touched})
