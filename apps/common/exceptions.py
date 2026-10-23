"""Structured API errors — never leak sensitive internals."""
from rest_framework.views import exception_handler as drf_handler


def api_exception_handler(exc, context):
    response = drf_handler(exc, context)
    if response is not None:
        response.data = {
            "error": True,
            "code": getattr(exc, "default_code", "error"),
            "message": _flatten(response.data),
            "details": response.data,
        }
    return response


def _flatten(data):
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            if isinstance(v, list):
                parts.append(f"{k}: {'; '.join(str(x) for x in v)}")
            else:
                parts.append(f"{k}: {v}")
        return "; ".join(parts) if parts else "Request failed"
    if isinstance(data, list):
        return "; ".join(str(x) for x in data)
    return str(data)
