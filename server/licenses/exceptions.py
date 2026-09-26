from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    ParseError,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler


class LicenseServiceError(Exception):
    code = "invalid_request"
    status_code = status.HTTP_400_BAD_REQUEST
    public_message = "The request could not be processed."


class InvalidLicenseError(LicenseServiceError):
    code = "invalid_license"
    status_code = status.HTTP_401_UNAUTHORIZED
    public_message = "The license or device could not be validated."


class DeviceBindingError(LicenseServiceError):
    code = "device_binding_rejected"
    status_code = status.HTTP_403_FORBIDDEN
    public_message = "The license is not available for this device."


class ReplayDetectedError(LicenseServiceError):
    code = "replay_detected"
    status_code = status.HTTP_409_CONFLICT
    public_message = "The request has already been processed."


class IdempotencyConflictError(LicenseServiceError):
    code = "idempotency_conflict"
    status_code = status.HTTP_409_CONFLICT
    public_message = "The idempotency key was already used for another request."


class ServiceConfigurationError(LicenseServiceError):
    code = "service_unavailable"
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    public_message = "The license service is temporarily unavailable."


def error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def api_exception_handler(exc, context):
    if isinstance(exc, LicenseServiceError):
        response = Response(
            error_payload(exc.code, exc.public_message),
            status=exc.status_code,
        )
        response["Cache-Control"] = "no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response
    response = exception_handler(exc, context)
    if response is None:
        return Response(
            error_payload(
                "internal_error", "The service could not complete the request."
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    if isinstance(exc, Throttled):
        code = "rate_limited"
        message = "Too many requests."
        response_status = status.HTTP_429_TOO_MANY_REQUESTS
    elif isinstance(exc, (ValidationError, ParseError)):
        code = "invalid_request"
        message = "The request could not be processed."
        response_status = status.HTTP_400_BAD_REQUEST
    elif (
        isinstance(exc, APIException)
        and exc.status_code == status.HTTP_401_UNAUTHORIZED
    ):
        code = "unauthorized"
        message = "Authentication is required."
        response_status = status.HTTP_401_UNAUTHORIZED
    elif isinstance(exc, APIException) and exc.status_code == status.HTTP_403_FORBIDDEN:
        code = "forbidden"
        message = "The request is not permitted."
        response_status = status.HTTP_403_FORBIDDEN
    elif isinstance(exc, APIException) and exc.status_code == status.HTTP_404_NOT_FOUND:
        code = "not_found"
        message = "The requested resource was not found."
        response_status = status.HTTP_404_NOT_FOUND
    elif isinstance(exc, APIException):
        code = (
            "method_not_allowed"
            if exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
            else "request_failed"
        )
        message = "The request could not be processed."
        response_status = exc.status_code
    else:
        code = "request_failed"
        message = "The request could not be processed."
        response_status = getattr(response, "status_code", status.HTTP_400_BAD_REQUEST)
    response.data = error_payload(code, message)
    response.status_code = response_status
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
