def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def log_audit(request, action, module, record="", old=None, new=None):
    from .models import AuditLog
    try:
        user = getattr(request, "user", None)
        AuditLog.objects.create(
            user=user if user and user.is_authenticated else None,
            action=action, module=module, record=record or "",
            old_value=old or {}, new_value=new or {},
            ip_address=_client_ip(request) if request else None,
        )
    except Exception:
        pass  # audit must never break the request
