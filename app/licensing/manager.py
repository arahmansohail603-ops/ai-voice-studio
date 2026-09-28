from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .client import LicenseHttpClient
from .crypto import parse_time, verify_lease
from .device import get_device_id
from .errors import (
    LicenseConfigurationError,
    LicenseExpiredError,
    LicenseNetworkError,
    LicenseServerError,
)
from .store import EncryptedLicenseStore


class LicenseManager:
    def __init__(
        self,
        client: LicenseHttpClient,
        store: EncryptedLicenseStore,
        device_id: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.device_id = device_id or get_device_id()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _load(self, *, allow_expired: bool = False) -> dict | None:
        state = self.store.load()
        if state is None:
            return None
        if state.get("version") != 1:
            raise LicenseConfigurationError(
                "The local license state version is invalid"
            )
        if state.get("device_id") != self.device_id:
            raise LicenseConfigurationError(
                "The local license belongs to another device"
            )
        license_key = state.get("license_key")
        if not isinstance(license_key, str) or not license_key:
            raise LicenseConfigurationError("The local license key is missing")
        envelope = state.get("envelope")
        if not isinstance(envelope, dict):
            raise LicenseConfigurationError("The local license response is invalid")
        last_server_value = state.get("last_server_time")
        if not isinstance(last_server_value, str) or not last_server_value:
            raise LicenseConfigurationError("The local license timestamp is missing")
        try:
            last_server_time = parse_time(last_server_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise LicenseConfigurationError(
                "The local license timestamp is invalid"
            ) from exc
        verify_lease(
            envelope,
            self.client.public_keys,
            self.device_id,
            now=self._now(),
            last_server_time=last_server_time,
            allow_expired=allow_expired,
            expected_license_key_id=state.get("license_key_id"),
            expected_activation_id=state.get("activation_id"),
        )
        return state

    def current(self) -> dict | None:
        return self._load()

    def activate(
        self, license_key: str, metadata: dict[str, Any] | None = None
    ) -> dict:
        key = str(license_key or "").strip().upper()
        if not key:
            raise LicenseConfigurationError("Enter a license key")
        payload = self.client.activate(
            key,
            self.device_id,
            {**(metadata or {}), "product": "ai-voice-studio", "platform": "desktop"},
        )
        envelope = payload.get("envelope") if isinstance(payload, dict) else None
        verified = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict) or not isinstance(verified, dict):
            raise LicenseConfigurationError("The license server returned no lease")
        state = {
            "version": 1,
            "license_key": key,
            "device_id": self.device_id,
            "activation_id": verified.get("activation_id"),
            "license_key_id": verified.get("license_key_id"),
            "envelope": envelope,
            "last_server_time": payload.get("_server_time"),
            "saved_at": self._now().isoformat(),
        }
        if not state["last_server_time"]:
            raise LicenseConfigurationError("The license server returned no timestamp")
        self.store.save(state)
        return state

    def refresh(self) -> dict:
        state = self._load(allow_expired=True)
        if state is None:
            raise LicenseConfigurationError("No local license is available")
        try:
            payload = self.client.validate(
                state["license_key"],
                self.device_id,
                last_server_time=state["last_server_time"],
                expected_license_key_id=state.get("license_key_id"),
                expected_activation_id=state.get("activation_id"),
            )
        except LicenseNetworkError as exc:
            if self._offline_allowed(state):
                return state
            # Chain the cause: a support bundle with only
            # "The license lease has expired and the server is unavailable"
            # cannot be told apart from a genuinely expired lease, and this is
            # the branch that decides whether the user loses the app.
            raise LicenseExpiredError(
                "The license lease has expired and the server is unavailable"
            ) from exc
        except LicenseServerError as exc:
            # The server only issues a fresh lease while the previous one is
            # still inside its grace window. After that it asks for a new
            # activation, which reuses the bound device and keeps the stored
            # key working without asking the user to type it again.
            if exc.code != "invalid_license":
                raise
            return self.activate(state["license_key"])
        envelope = payload.get("envelope") if isinstance(payload, dict) else None
        verified = payload.get("payload") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict) or not isinstance(verified, dict):
            raise LicenseConfigurationError("The license server returned no lease")
        new_state = dict(state)
        new_state.update(
            {
                "activation_id": verified.get("activation_id"),
                "license_key_id": verified.get("license_key_id"),
                "envelope": envelope,
                "last_server_time": payload.get("_server_time"),
                "saved_at": self._now().isoformat(),
            }
        )
        if not new_state["last_server_time"]:
            raise LicenseConfigurationError("The license server returned no timestamp")
        self.store.save(new_state)
        return new_state

    def _offline_allowed(self, state: dict) -> bool:
        try:
            envelope = state["envelope"]
            last_server_time = parse_time(state["last_server_time"])
            payload = verify_lease(
                envelope,
                self.client.public_keys,
                self.device_id,
                now=self._now(),
                last_server_time=last_server_time,
                allow_expired=True,
                expected_license_key_id=state.get("license_key_id"),
                expected_activation_id=state.get("activation_id"),
            )
            current = max(self._now(), last_server_time)
            return current <= parse_time(payload["grace_expires_at"])
        except (LicenseExpiredError, ValueError, TypeError, KeyError, OverflowError):
            return False

    def clear(self) -> None:
        self.store.clear()

    def remaining_seconds(self) -> int:
        state = self._load(allow_expired=True)
        if state is None:
            return 0
        expires = parse_time(state["envelope"]["payload"]["grace_expires_at"])
        current = max(self._now(), parse_time(state["last_server_time"]))
        return max(0, int((expires - current).total_seconds()))
