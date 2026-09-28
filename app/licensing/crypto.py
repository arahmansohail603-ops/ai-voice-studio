from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import LicenseExpiredError

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:
    InvalidSignature = Exception
    serialization = None
    Ed25519PublicKey = None


_MAX_CLOCK_SKEW = timedelta(minutes=5)
_DEVICE_BINDING_CONTEXT = b"ai-voice-studio-device-binding-v1\0"


def device_binding(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Device identity is missing")
    normalized = value.strip()
    return hashlib.sha256(
        _DEVICE_BINDING_CONTEXT + normalized.encode("utf-8")
    ).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid base64url value")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64url value") from exc


def _decode_public_material(material: Any) -> Ed25519PublicKey:
    if Ed25519PublicKey is None or serialization is None:
        raise RuntimeError("cryptography is required for license validation")
    if isinstance(material, Ed25519PublicKey):
        return material
    if isinstance(material, bytes) and len(material) == 32:
        return Ed25519PublicKey.from_public_bytes(material)
    if not isinstance(material, str) or not material.strip():
        raise ValueError("Invalid public key material")
    value = material.strip()
    if "BEGIN" in value:
        loaded = serialization.load_pem_public_key(value.encode("utf-8"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("The configured key is not Ed25519")
        return loaded
    candidates: list[bytes] = []
    try:
        candidates.append(b64url_decode(value))
    except ValueError:
        pass
    try:
        candidates.append(
            base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        )
    except (binascii.Error, ValueError):
        pass
    try:
        if len(value) % 2 == 0:
            candidates.append(bytes.fromhex(value))
    except ValueError:
        pass
    for candidate in candidates:
        if len(candidate) == 32:
            return Ed25519PublicKey.from_public_bytes(candidate)
    raise ValueError("Public key must be a 32-byte Ed25519 key")


def parse_public_keys(value: Any) -> dict[str, Ed25519PublicKey]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("License public keys must be valid JSON") from exc
    if isinstance(value, list):
        value = {
            str(item.get("key_id")): item.get("public_key")
            for item in value
            if isinstance(item, dict) and item.get("key_id") and item.get("public_key")
        }
    if not isinstance(value, dict) or not value:
        raise ValueError("At least one license public key is required")
    return {
        str(key_id): _decode_public_material(material)
        for key_id, material in value.items()
    }


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("Lease timestamp is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Lease timestamp is not a valid date: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        # A UTC offset can push the instant outside the representable range,
        # e.g. "0001-01-01T00:00:00+10:00". OverflowError is an ArithmeticError,
        # not a ValueError, so it used to escape the licensing gate as an
        # unhandled crash -- and because the shipped build has the console
        # disabled, that killed the app silently on double-click.
        raise ValueError(f"Lease timestamp is out of range: {value!r}") from exc


def _constant_time_equals(left: Any, right: Any) -> bool:
    """Constant-time string compare that answers False instead of raising.

    ``hmac.compare_digest`` only accepts ``str`` when it is ASCII-only, and
    raises ``TypeError`` otherwise. A lease whose ``license_key_id`` or
    ``device_binding`` carries a non-ASCII character therefore crashed the
    client instead of being rejected. The server only ever emits ASCII ids, so
    a non-ASCII one means a malformed or tampered response.
    """
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if not left.isascii() or not right.isascii():
        return False
    return hmac.compare_digest(left, right)


def verify_lease(
    envelope: dict[str, Any],
    public_keys: dict[str, Any],
    expected_device_id: str,
    now: datetime | None = None,
    last_server_time: datetime | None = None,
    allow_expired: bool = False,
    expected_license_key_id: str | None = None,
    expected_activation_id: str | None = None,
    require_newer_server_time: bool = False,
) -> dict[str, Any]:
    if Ed25519PublicKey is None or serialization is None:
        raise RuntimeError("cryptography is required for license validation")
    if not isinstance(envelope, dict):
        raise ValueError("License response is not an object")
    if (
        envelope.get("algorithm") != "Ed25519"
        or envelope.get("encoding") != "canonical-json"
    ):
        raise ValueError("Unsupported license response format")
    key_id = envelope.get("key_id")
    signature = envelope.get("signature")
    payload = envelope.get("payload")
    if (
        not isinstance(key_id, str)
        or not isinstance(signature, str)
        or not isinstance(payload, dict)
    ):
        raise ValueError("License response is incomplete")
    for field in ("lease_id", "license_key_id", "activation_id"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError("License response identity is incomplete")
    if expected_license_key_id is not None and not _constant_time_equals(
        payload["license_key_id"], str(expected_license_key_id)
    ):
        raise ValueError("License response identity does not match")
    if expected_activation_id is not None and not _constant_time_equals(
        payload["activation_id"], str(expected_activation_id)
    ):
        raise ValueError("License response activation does not match")
    keys = parse_public_keys(public_keys)
    public_key = keys.get(key_id)
    if public_key is None:
        raise ValueError("License response used an unknown signing key")
    try:
        public_key.verify(
            b64url_decode(signature), canonical_json(payload).encode("utf-8")
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise ValueError("License response signature is invalid") from exc
    if payload.get("type") != "license_lease" or payload.get("version") != 1:
        raise ValueError("License response version is invalid")
    if payload.get("valid") is not True or payload.get("state") not in {
        "active",
        "grace",
    }:
        raise ValueError("License response is not active")
    if not isinstance(expected_device_id, str) or not expected_device_id:
        raise ValueError("Device identity is missing")
    expected_binding = device_binding(expected_device_id)
    if not _constant_time_equals(
        payload.get("device_binding", ""), expected_binding
    ):
        raise ValueError("License response is not bound to this device")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued_at = parse_time(payload.get("issued_at"))
    expires_at = parse_time(payload.get("expires_at"))
    grace_expires_at = parse_time(payload.get("grace_expires_at"))
    server_time = parse_time(payload.get("server_time"))
    # The response carries its own ``server_time``, and because the payload is
    # Ed25519-signed that is the one timestamp here no local change can move.
    # Folding it into "now" is what keeps a machine whose clock runs slow from
    # reading every single response as "dated in the future" -- which showed a
    # paying customer an activation dialog that no key could ever satisfy, so
    # the app could not start at all. It can only push "now" later, never
    # earlier, so it can never be used to extend a lease.
    if last_server_time is not None:
        current = max(current, last_server_time)
    current = max(current, server_time)
    if issued_at > current + _MAX_CLOCK_SKEW:
        raise ValueError("License response is dated in the future")
    if grace_expires_at < expires_at:
        raise ValueError("License lease timestamps are invalid")
    lease_duration = payload.get("lease_duration_seconds")
    grace_duration = payload.get("grace_duration_seconds")
    if (
        type(lease_duration) is not int
        or lease_duration < 0
        or type(grace_duration) is not int
        or grace_duration < 0
    ):
        raise ValueError("License lease duration is invalid")
    if not allow_expired:
        if current > grace_expires_at:
            raise LicenseExpiredError("License lease has expired")
        if payload.get("state") == "active" and current > expires_at:
            raise LicenseExpiredError("License lease is no longer active")
    if last_server_time is not None:
        if require_newer_server_time and server_time <= last_server_time:
            raise ValueError(
                "License server response is not newer than the previous lease"
            )
        if server_time < last_server_time - _MAX_CLOCK_SKEW:
            raise ValueError("License server clock moved backwards")
    verified_payload = dict(payload)
    verified_payload["_verified_at"] = current.isoformat()
    verified_payload["_server_time"] = server_time.isoformat()
    return verified_payload


def digest_bytes(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def constant_time_equal(left: bytes, right: bytes) -> bool:
    return hmac.compare_digest(left, right)
