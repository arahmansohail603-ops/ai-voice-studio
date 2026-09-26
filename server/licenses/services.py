from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from datetime import timezone as datetime_timezone

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.utils import timezone

from .crypto import (
    canonical_json,
    hash_device_id,
    hash_license_key,
    hash_opaque_token,
    public_device_binding,
    sign_payload,
)
from .exceptions import (
    DeviceBindingError,
    IdempotencyConflictError,
    InvalidLicenseError,
    ReplayDetectedError,
    ServiceConfigurationError,
    error_payload,
)
from .models import (
    AuditEvent,
    DeviceActivation,
    IdempotencyRecord,
    LicenseKey,
    LicenseLease,
    RequestNonce,
)


@dataclass
class RequestContext:
    endpoint: str
    ip_address: str | None
    user_agent: str
    nonce_hash: str = ""
    request_fingerprint: str = ""


@dataclass
class ServiceResult:
    license_key: LicenseKey
    activation: DeviceActivation
    lease: LicenseLease
    state: str
    created: bool
    envelope: dict


def compact_json(value: dict) -> str:
    return canonical_json(value)


def _require_positive_setting(name: str, minimum: int = 1) -> int:
    value = getattr(settings, name, None)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ImproperlyConfigured(f"{name} must be an integer of at least {minimum}")
    return value


def request_fingerprint(data: dict) -> str:
    semantic_data = {
        key: value
        for key, value in data.items()
        if key not in {"nonce", "idempotency_key"}
    }
    return hash_opaque_token("request-fingerprint-v2\0" + canonical_json(semantic_data))


def _timestamp(value) -> str:
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")


def _trim_lease_history(activation: DeviceActivation, current_id=None) -> None:
    keep = _require_positive_setting("LICENSE_LEASE_RETENTION_COUNT", 1)
    leases = LicenseLease.objects.filter(device_activation=activation)
    if current_id is not None:
        leases = leases.exclude(pk=current_id)
    old_ids = list(
        leases.order_by("-issued_at", "-id").values_list("id", flat=True)[
            max(0, keep - 1) :
        ]
    )
    if old_ids:
        LicenseLease.objects.filter(id__in=old_ids).delete()


def _lease_policy() -> tuple[int, int]:
    try:
        duration = int(settings.LICENSE_LEASE_DURATION_SECONDS)
        grace = int(settings.LICENSE_LEASE_GRACE_SECONDS)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ImproperlyConfigured("Lease duration and grace must be integers") from exc
    if duration < 1:
        raise ImproperlyConfigured("License lease duration must be positive")
    if grace < 0 or grace > 86400:
        raise ImproperlyConfigured("License lease grace is outside the allowed range")
    return duration, grace


def record_audit(
    event_type: str,
    context: RequestContext | None = None,
    license_key: LicenseKey | None = None,
    activation: DeviceActivation | None = None,
    actor=None,
    metadata: dict | None = None,
    nonce_hash: str = "",
    request_hash: str = "",
) -> AuditEvent:
    return AuditEvent.objects.create(
        event_type=event_type[:64],
        license_key=license_key,
        device_activation=activation,
        actor=actor,
        nonce_hash=(nonce_hash or (context.nonce_hash if context else ""))[:64],
        request_fingerprint=(
            request_hash or (context.request_fingerprint if context else "")
        )[:64],
        ip_address=context.ip_address if context else None,
        user_agent=(context.user_agent[:512] if context else ""),
        metadata=metadata or {},
    )


def _attach_audit_target(
    error: Exception,
    license_key: LicenseKey | None = None,
    activation: DeviceActivation | None = None,
    event_type: str = "license_denied",
) -> Exception:
    error.audit_license_key = license_key
    error.audit_activation = activation
    error.audit_license_key_id = str(license_key.id) if license_key else ""
    error.audit_activation_id = str(activation.id) if activation else ""
    error.audit_event_type = event_type
    return error


def _license_for_hash(key_hash: str) -> LicenseKey | None:
    return LicenseKey.objects.select_for_update().filter(key_hash=key_hash).first()


def _lease_payload(
    lease: LicenseLease,
    state: str,
    server_time,
    device_id: str,
) -> dict:
    license_key = lease.license_key
    activation = lease.device_activation
    lease_duration = max(0, int((lease.expires_at - lease.issued_at).total_seconds()))
    grace_duration = max(
        0, int((lease.grace_expires_at - lease.expires_at).total_seconds())
    )
    return {
        "activation_id": str(activation.id),
        "client_metadata": activation.metadata
        if isinstance(activation.metadata, dict)
        else {},
        "device_binding": public_device_binding(device_id),
        "entitlements": license_key.metadata
        if isinstance(license_key.metadata, dict)
        else {},
        "expires_at": _timestamp(lease.expires_at),
        "grace_expires_at": _timestamp(lease.grace_expires_at),
        "grace_duration_seconds": grace_duration,
        "issued_at": _timestamp(lease.issued_at),
        "lease_duration_seconds": lease_duration,
        "lease_id": str(lease.id),
        "license_key_id": str(license_key.id),
        "server_time": _timestamp(server_time),
        "state": state,
        "type": "license_lease",
        "valid": True,
        "version": 1,
    }


def _signed_lease_envelope(
    lease: LicenseLease, state: str, server_time, device_id: str
) -> dict:
    return sign_payload(_lease_payload(lease, state, server_time, device_id))


def _create_lease(
    license_key: LicenseKey,
    activation: DeviceActivation,
    now,
    device_id: str,
) -> tuple[LicenseLease, dict]:
    duration, grace = _lease_policy()
    lease_id = uuid.uuid4()
    expires_at = now + timedelta(seconds=duration)
    grace_expires_at = expires_at + timedelta(seconds=grace)
    payload = {
        "activation_id": str(activation.id),
        "client_metadata": activation.metadata
        if isinstance(activation.metadata, dict)
        else {},
        "device_binding": public_device_binding(device_id),
        "entitlements": license_key.metadata
        if isinstance(license_key.metadata, dict)
        else {},
        "expires_at": _timestamp(expires_at),
        "grace_expires_at": _timestamp(grace_expires_at),
        "grace_duration_seconds": grace,
        "issued_at": _timestamp(now),
        "lease_duration_seconds": duration,
        "lease_id": str(lease_id),
        "license_key_id": str(license_key.id),
        "server_time": _timestamp(now),
        "state": LicenseLease.ACTIVE,
        "type": "license_lease",
        "valid": True,
        "version": 1,
    }
    envelope = sign_payload(payload)
    lease = LicenseLease.objects.create(
        id=lease_id,
        license_key=license_key,
        device_activation=activation,
        issued_at=now,
        expires_at=expires_at,
        grace_expires_at=grace_expires_at,
        state=LicenseLease.ACTIVE,
        signing_key_id=envelope["key_id"],
        canonical_payload=canonical_json(payload),
        signature=envelope["signature"],
        payload=payload,
    )
    return lease, envelope


def _get_activation(license_key: LicenseKey) -> DeviceActivation | None:
    return (
        DeviceActivation.objects.select_for_update()
        .filter(license_key=license_key)
        .first()
    )


def activate_license(
    raw_key: str,
    device_id: str,
    metadata: dict | None = None,
    context: RequestContext | None = None,
) -> ServiceResult:
    key_hash = hash_license_key(raw_key)
    device_hash = hash_device_id(device_id)
    now = timezone.now()
    with transaction.atomic():
        license_key = _license_for_hash(key_hash)
        if license_key is None:
            raise _attach_audit_target(InvalidLicenseError())
        if license_key.is_revoked:
            raise _attach_audit_target(InvalidLicenseError(), license_key=license_key)
        activation = _get_activation(license_key)
        created = activation is None
        if activation is None:
            activation = DeviceActivation.objects.create(
                license_key=license_key,
                device_id_hash=device_hash,
                device_id_hint=device_hash[:16],
                metadata=metadata or {},
                status=DeviceActivation.ACTIVE,
                last_seen_at=now,
            )
        else:
            if activation.status != DeviceActivation.ACTIVE or not hmac.compare_digest(
                activation.device_id_hash, device_hash
            ):
                raise _attach_audit_target(
                    DeviceBindingError(),
                    license_key=license_key,
                    activation=activation,
                )
        lease, envelope = _create_lease(license_key, activation, now, device_id)
        _trim_lease_history(activation, lease.id)
        activation.last_seen_at = now
        activation.save(update_fields=("last_seen_at",))
        record_audit(
            "activation_created" if created else "activation_reused",
            context=context,
            license_key=license_key,
            activation=activation,
            metadata={"lease_id": str(lease.id)},
        )
    return ServiceResult(
        license_key, activation, lease, LicenseLease.ACTIVE, created, envelope
    )


def validate_license(
    raw_key: str,
    device_id: str,
    context: RequestContext | None = None,
) -> ServiceResult:
    key_hash = hash_license_key(raw_key)
    device_hash = hash_device_id(device_id)
    now = timezone.now()
    with transaction.atomic():
        license_key = _license_for_hash(key_hash)
        if license_key is None:
            raise _attach_audit_target(InvalidLicenseError())
        if license_key.is_revoked:
            raise _attach_audit_target(InvalidLicenseError(), license_key=license_key)
        activation = _get_activation(license_key)
        if activation is None:
            raise _attach_audit_target(InvalidLicenseError(), license_key=license_key)
        if activation.status != DeviceActivation.ACTIVE or not hmac.compare_digest(
            activation.device_id_hash, device_hash
        ):
            raise _attach_audit_target(
                DeviceBindingError(),
                license_key=license_key,
                activation=activation,
            )
        lease = (
            LicenseLease.objects.filter(device_activation=activation)
            .select_related("license_key", "device_activation")
            .order_by("-issued_at", "-id")
            .first()
        )
        if lease is None or now <= lease.expires_at:
            lease, envelope = _create_lease(license_key, activation, now, device_id)
            _trim_lease_history(activation, lease.id)
            state = LicenseLease.ACTIVE
            event_type = "status_valid"
        elif now <= lease.grace_expires_at:
            state = LicenseLease.GRACE
            lease.state = LicenseLease.GRACE
            lease.save(update_fields=("state",))
            envelope = _signed_lease_envelope(lease, state, now, device_id)
            event_type = "status_grace"
        else:
            raise _attach_audit_target(
                InvalidLicenseError(),
                license_key=license_key,
                activation=activation,
                event_type="status_expired",
            )
        activation.last_seen_at = now
        activation.save(update_fields=("last_seen_at",))
        record_audit(
            event_type,
            context=context,
            license_key=license_key,
            activation=activation,
            metadata={"lease_id": str(lease.id), "state": state},
        )
    return ServiceResult(license_key, activation, lease, state, False, envelope)


def _error_response(error: Exception) -> tuple[int, dict]:
    return (
        getattr(error, "status_code", 400),
        error_payload(
            getattr(error, "code", "invalid_request"),
            getattr(error, "public_message", "The request could not be processed."),
        ),
    )


def _record_denied_audit(
    error: Exception,
    context: RequestContext,
    nonce_hash: str,
    request_hash: str,
) -> None:
    event_type = getattr(error, "audit_event_type", "license_denied")
    AuditEvent.objects.create(
        event_type=event_type[:64],
        license_key=getattr(error, "audit_license_key", None),
        device_activation=getattr(error, "audit_activation", None),
        nonce_hash=nonce_hash,
        request_fingerprint=request_hash,
        ip_address=context.ip_address,
        user_agent=context.user_agent[:512],
        metadata={},
    )


def execute_idempotent(
    endpoint: str,
    data: dict,
    context: RequestContext,
    operation: Callable[[], ServiceResult],
) -> tuple[int, str]:
    request_hash = request_fingerprint(data)
    nonce = str(data["nonce"])
    idempotency_key = str(data["idempotency_key"])
    try:
        nonce_ttl = _require_positive_setting("LICENSE_NONCE_TTL_SECONDS", 1)
        idempotency_ttl = _require_positive_setting(
            "LICENSE_IDEMPOTENCY_TTL_SECONDS", 1
        )
    except ImproperlyConfigured:
        status, body = _error_response(ServiceConfigurationError())
        return status, compact_json(body)
    nonce_hash = hash_opaque_token(f"{endpoint}\0{nonce}")
    idempotency_hash = hash_opaque_token(f"{endpoint}\0{idempotency_key}")
    context.nonce_hash = nonce_hash
    context.request_fingerprint = request_hash
    now = timezone.now()
    nonce_expiry = now + timedelta(seconds=nonce_ttl)
    idempotency_expiry = now + timedelta(seconds=idempotency_ttl)
    try:
        with transaction.atomic():
            RequestNonce.objects.filter(expires_at__lte=now).delete()
            IdempotencyRecord.objects.filter(expires_at__lte=now).delete()
            existing = (
                IdempotencyRecord.objects.select_for_update()
                .filter(
                    endpoint=endpoint,
                    idempotency_key_hash=idempotency_hash,
                    expires_at__gt=now,
                )
                .first()
            )
            if existing is not None:
                if not hmac.compare_digest(existing.request_fingerprint, request_hash):
                    raise IdempotencyConflictError()
                return existing.response_status, existing.response_body
            prior_nonce = (
                RequestNonce.objects.select_for_update()
                .filter(endpoint=endpoint, nonce_hash=nonce_hash, expires_at__gt=now)
                .first()
            )
            if prior_nonce is not None:
                raise ReplayDetectedError()
            RequestNonce.objects.create(
                endpoint=endpoint,
                nonce_hash=nonce_hash,
                request_fingerprint=request_hash,
                expires_at=nonce_expiry,
            )
            try:
                result = operation()
                response_status = 200
                response_data = result.envelope
            except ImproperlyConfigured:
                response_status, response_data = _error_response(
                    ServiceConfigurationError()
                )
            except Exception as error:
                from .exceptions import LicenseServiceError

                if not isinstance(error, LicenseServiceError):
                    raise
                response_status, response_data = _error_response(error)
                _record_denied_audit(error, context, nonce_hash, request_hash)
            body = compact_json(response_data)
            IdempotencyRecord.objects.create(
                endpoint=endpoint,
                idempotency_key_hash=idempotency_hash,
                request_fingerprint=request_hash,
                response_status=response_status,
                response_body=body,
                expires_at=idempotency_expiry,
            )
            return response_status, body
    except IntegrityError:
        existing = IdempotencyRecord.objects.filter(
            endpoint=endpoint,
            idempotency_key_hash=idempotency_hash,
            expires_at__gt=timezone.now(),
        ).first()
        if existing is not None:
            if hmac.compare_digest(existing.request_fingerprint, request_hash):
                return existing.response_status, existing.response_body
            raise IdempotencyConflictError()
        raise ReplayDetectedError()
