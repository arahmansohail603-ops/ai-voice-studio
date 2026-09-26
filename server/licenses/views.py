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
