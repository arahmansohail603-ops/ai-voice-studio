from __future__ import annotations

import ipaddress
from typing import ClassVar

from django.db import connection
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from .crypto import canonical_json, get_private_key, hmac_pepper
from .exceptions import ServiceConfigurationError
from .serializers import ActivationRequestSerializer, StatusRequestSerializer
from .services import (
    RequestContext,
    activate_license,
    execute_idempotent,
    validate_license,
)
from .throttling import ActivationRateThrottle, HealthRateThrottle, StatusRateThrottle


def _response(body: str, status: int = 200) -> HttpResponse:
    response = HttpResponse(body, status=status, content_type="application/json")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _request_data(request) -> dict:
    data = request.data
    if not hasattr(data, "get"):
        raise ValidationError("A JSON object is required.")
    try:
        merged = data.copy()
    except AttributeError as exc:
        raise ValidationError("A JSON object is required.") from exc
    for field, header_names in {
        "nonce": ("HTTP_X_NONCE", "HTTP_X_CLIENT_NONCE"),
        "idempotency_key": (
            "HTTP_X_IDEMPOTENCY_KEY",
            "HTTP_IDEMPOTENCY_KEY",
        ),
    }.items():
        header_value = next(
            (request.META.get(name) for name in header_names if request.META.get(name)),
            None,
        )
        body_value = merged.get(field)
        if body_value and header_value and str(body_value) != str(header_value):
            raise ValidationError("Conflicting request authentication fields.")
        if header_value and not body_value:
            merged[field] = header_value
    return merged


def _request_context(request, endpoint: str) -> RequestContext:
    ip_address = request.META.get("REMOTE_ADDR")
    from django.conf import settings

    if getattr(settings, "LICENSE_TRUST_PROXY_HEADERS", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            ip_address = forwarded.split(",")[0].strip()
    try:
        if ip_address:
            ip_address = str(ipaddress.ip_address(ip_address))
    except ValueError:
        ip_address = None
    user_agent = request.META.get("HTTP_USER_AGENT", "")
    return RequestContext(endpoint, ip_address, user_agent[:512])


def _run_client_request(request, endpoint: str, serializer_class, operation):
    data = _request_data(request)
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    validated = serializer.validated_data
    context = _request_context(request, endpoint)
    status, body = execute_idempotent(
        endpoint,
        validated,
        context,
        lambda: operation(validated, context),
    )
    return _response(body, status)


class ActivationView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]
    throttle_classes: ClassVar[list] = [ActivationRateThrottle]
    throttle_scope = "license_activation"

    def post(self, request):
        return _run_client_request(
            request,
            "license.activate",
            ActivationRequestSerializer,
            lambda data, context: activate_license(
                data["license_key"],
                data["device_id"],
                data.get("metadata", {}),
                context,
            ),
        )


class StatusView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]
    throttle_classes: ClassVar[list] = [StatusRateThrottle]
    throttle_scope = "license_status"

    def post(self, request):
        return _run_client_request(
            request,
            "license.status",
            StatusRequestSerializer,
            lambda data, context: validate_license(
                data["license_key"],
                data["device_id"],
                context,
            ),
        )


class ValidateView(StatusView):
    pass


class HealthView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]
    throttle_classes: ClassVar[list] = [HealthRateThrottle]
    throttle_scope = "health"

    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            hmac_pepper()
            key_id, _ = get_private_key()
        except Exception as exc:
            raise ServiceConfigurationError() from exc

        payload = {
            "key_id": key_id,
            "service": "license-server",
            "status": "ok",
            "time": timezone.now().isoformat().replace("+00:00", "Z"),
            "version": 1,
        }
        return _response(canonical_json(payload))


class LandingView(APIView):
    authentication_classes: ClassVar[list] = []
    permission_classes: ClassVar[list] = [AllowAny]

    def get(self, request):
        html = (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>AI Voice Studio License Server</title><style>"
            "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
            "background:#0f172a;color:#e2e8f0;margin:0;display:flex;"
            "min-height:100vh;align-items:center;justify-content:center}"
            ".card{background:#1e293b;border:1px solid #334155;border-radius:16px;"
            "padding:40px 48px;max-width:560px;text-align:center;box-shadow:"
            "0 20px 40px rgba(0,0,0,.4)}h1{font-size:26px;margin:0 0 8px;"
            "color:#f8fafc}code{background:#0f172a;padding:2px 8px;border-radius:6px;"
            "color:#7dd3fc}.status{display:inline-flex;align-items:center;gap:8px;"
            "color:#4ade80;font-weight:600;margin:16px 0} .dot{width:10px;height:10px;"
            "border-radius:50%;background:#4ade80;animation:pulse 2s infinite}"
            "@keyframes pulse{50%{opacity:.3}}p{font-size:14px;color:#94a3b8;"
            "margin:6px 0}.links{margin-top:20px;display:flex;gap:12px;"
            "justify-content:center;flex-wrap:wrap}.links a{color:#38bdf8;"
            "text-decoration:none;font-size:14px;border:1px solid #334155;"
            "border-radius:8px;padding:8px 14px} .links a:hover{background:#334155}"
            "</style></head><body><div class='card'>"
            "<h1>AI Voice Studio</h1>"
            "<div class='status'><span class='dot'></span>License server is running</div>"
            "<p>Service: <code>license-server</code></p>"
            "<p>Signing key: <code>local-v1</code></p>"
            "<div class='links'>"
            "<a href='health/'>Health</a>"
            "<a href='api/v1/health/'>API Health</a>"
            "</div></div></body></html>"
        )
        response = HttpResponse(html, status=200, content_type="text/html")
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
