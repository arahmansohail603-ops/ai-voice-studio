from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

_ephemeral_keys: dict[str, Ed25519PrivateKey] = {}
_private_key_cache: dict[tuple[str, str], Ed25519PrivateKey] = {}
_ephemeral_lock = threading.Lock()
_DEVICE_BINDING_CONTEXT = b"ai-voice-studio-device-binding-v1\0"


def normalize_license_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("License key must be text")
    normalized = value.strip().upper()
    if not normalized:
        raise ValueError("License key must not be empty")
    return normalized


def validate_license_key_strength(value: str) -> str:
    normalized = normalize_license_key(value)
    if len(normalized) < 20:
        raise ValueError("Custom license keys must contain at least 20 characters")
    classes = (
        any(character.isalpha() for character in normalized),
        any(character.isdigit() for character in normalized),
        any(not character.isalnum() for character in normalized),
    )
    if sum(classes) < 2:
        raise ValueError("Custom license keys must mix letters, digits, or symbols")
    return normalized


def normalize_device_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Device identifier must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Device identifier must not be empty")
    return normalized


def hmac_pepper() -> bytes:
    value = getattr(settings, "LICENSE_HMAC_PEPPER", "")
    if not value:
        if not getattr(settings, "DEBUG", False):
            raise ImproperlyConfigured("LICENSE_HMAC_PEPPER must be configured")
        value = "local-development-only-hmac-pepper-change-me"
    if not getattr(settings, "DEBUG", False) and (
        len(value) < 32
        or "replace-with" in value.lower()
        or value == "local-development-only-hmac-pepper-change-me"
    ):
        raise ImproperlyConfigured("LICENSE_HMAC_PEPPER must be a strong unique secret")
    return value.encode("utf-8")


def hash_license_key(value: str) -> str:
    normalized = normalize_license_key(value)
    return hmac.new(
        hmac_pepper(), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hash_device_id(value: str) -> str:
    normalized = normalize_device_id(value)
    return hmac.new(
        hmac_pepper(), normalized.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def hash_opaque_token(value: str) -> str:
    return hmac.new(hmac_pepper(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def public_device_binding(value: str) -> str:
    normalized = normalize_device_id(value)
    return hashlib.sha256(
        _DEVICE_BINDING_CONTEXT + normalized.encode("utf-8")
    ).hexdigest()


def license_key_hint(value: str) -> str:
    normalized = normalize_license_key(value)
    return normalized[-4:]


def generate_license_key() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(5)]
    return "LIC-" + "-".join(groups)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64url value") from exc


def _configured_key_map() -> dict[str, Any]:
    configured = getattr(settings, "LICENSE_SIGNING_KEYS", {})
    if isinstance(configured, str):
        try:
            configured = json.loads(configured)
        except json.JSONDecodeError as exc:
            raise ImproperlyConfigured(
                "LICENSE_SIGNING_KEYS must be valid JSON"
            ) from exc
    if isinstance(configured, list):
        configured = {
            str(item.get("key_id")): item.get("private_key")
            for item in configured
            if isinstance(item, dict) and item.get("key_id") and item.get("private_key")
        }
    if (
        isinstance(configured, dict)
        and "private_key" in configured
        and "key_id" in configured
    ):
        configured = {str(configured["key_id"]): configured["private_key"]}
    if not configured:
        if not getattr(settings, "DEBUG", False):
            raise ImproperlyConfigured("LICENSE_SIGNING_KEYS must be configured")
        key_id = str(getattr(settings, "LICENSE_SIGNING_KEY_ID", "local-dev"))
        with _ephemeral_lock:
            private_key = _ephemeral_keys.setdefault(
                key_id, Ed25519PrivateKey.generate()
            )
        return {key_id: private_key}
    if not isinstance(configured, dict) or not configured:
        raise ImproperlyConfigured("LICENSE_SIGNING_KEYS must be a mapping")
    return {str(key_id): material for key_id, material in configured.items()}


def _decode_private_bytes(material: str) -> bytes:
    candidates: list[bytes] = []
    try:
        candidates.append(b64url_decode(material))
    except ValueError:
        pass
    try:
        padding = "=" * (-len(material) % 4)
        candidates.append(base64.b64decode(material + padding, validate=True))
    except (binascii.Error, ValueError):
        pass
    try:
        if len(material) % 2 == 0:
            candidates.append(bytes.fromhex(material))
    except ValueError:
        pass
    for candidate in candidates:
        if len(candidate) == 32:
            return candidate
    raise ImproperlyConfigured("Signing key material must be a 32-byte Ed25519 key")


def _load_private_key(material: Any) -> Ed25519PrivateKey:
    if isinstance(material, Ed25519PrivateKey):
        return material
    if not isinstance(material, str) or not material.strip():
        raise ImproperlyConfigured("Signing key material is empty")
    material = material.strip()
    if "BEGIN" in material:
        try:
            loaded = serialization.load_pem_private_key(
                material.encode("utf-8"), password=None
            )
        except (ValueError, TypeError) as exc:
            raise ImproperlyConfigured("Unable to load PEM signing key") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise ImproperlyConfigured("The configured signing key is not Ed25519")
        return loaded
    cache_key = ("raw", material)
    cached = _private_key_cache.get(cache_key)
    if cached is not None:
        return cached
    private_key = Ed25519PrivateKey.from_private_bytes(_decode_private_bytes(material))
    _private_key_cache[cache_key] = private_key
    return private_key


def get_private_key(key_id: str | None = None) -> tuple[str, Ed25519PrivateKey]:
    key_map = _configured_key_map()
    selected_id = str(
        key_id or getattr(settings, "LICENSE_SIGNING_KEY_ID", "local-dev")
    )
    if selected_id not in key_map:
        if len(key_map) == 1:
            selected_id = next(iter(key_map))
        else:
            raise ImproperlyConfigured(f"Unknown signing key id: {selected_id}")
    return selected_id, _load_private_key(key_map[selected_id])


def get_public_key(key_id: str | None = None) -> tuple[str, Ed25519PublicKey]:
    selected_id, private_key = get_private_key(key_id)
    return selected_id, private_key.public_key()


def public_key_bytes(key_id: str | None = None) -> tuple[str, bytes]:
    selected_id, public_key = get_public_key(key_id)
    return selected_id, public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign_payload(payload: dict[str, Any], key_id: str | None = None) -> dict[str, Any]:
    selected_id, private_key = get_private_key(key_id)
    canonical_payload = canonical_json(payload)
    signature = private_key.sign(canonical_payload.encode("utf-8"))
    return {
        "algorithm": "Ed25519",
        "encoding": "canonical-json",
        "key_id": selected_id,
        "payload": payload,
        "signature": b64url_encode(signature),
    }


def _public_key_from_material(material: Any) -> Ed25519PublicKey:
    if isinstance(material, Ed25519PublicKey):
        return material
    if isinstance(material, Ed25519PrivateKey):
        return material.public_key()
    if isinstance(material, bytes) and len(material) == 32:
        return Ed25519PublicKey.from_public_bytes(material)
    if not isinstance(material, str) or not material.strip():
        raise ValueError("Invalid public key material")
    material = material.strip()
    if "BEGIN" in material:
        loaded = serialization.load_pem_public_key(material.encode("utf-8"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("The configured public key is not Ed25519")
        return loaded
    try:
        raw = _decode_private_bytes(material)
    except ImproperlyConfigured as exc:
        raise ValueError("Invalid public key material") from exc
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_signed_response(
    envelope: dict[str, Any],
    public_keys: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "Ed25519":
        return False
    if envelope.get("encoding") != "canonical-json":
        return False
    key_id = envelope.get("key_id")
    signature = envelope.get("signature")
    payload = envelope.get("payload")
    if not isinstance(key_id, str) or not isinstance(signature, str):
        return False
    if not isinstance(payload, dict):
        return False
    if public_keys is None:
        try:
            _, public_key = get_public_key(key_id)
        except (ImproperlyConfigured, ValueError):
            return False
    else:
        material = public_keys.get(key_id)
        if material is None:
            return False
        try:
            public_key = _public_key_from_material(material)
        except (ValueError, TypeError):
            return False
    try:
        public_key.verify(
            b64url_decode(signature), canonical_json(payload).encode("utf-8")
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
