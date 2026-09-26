from __future__ import annotations

import http.client
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .crypto import parse_public_keys, parse_time, verify_lease
from .errors import LicenseConfigurationError, LicenseNetworkError, LicenseServerError


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LicenseHttpClient:
    def __init__(
        self,
        base_url: str,
        public_keys: Any,
        timeout: float = 12.0,
        allow_insecure_localhost: bool = True,
    ) -> None:
        self.base_url = str(base_url).strip().rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise LicenseConfigurationError("License server URL is invalid")
        local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (allow_insecure_localhost and local):
            raise LicenseConfigurationError("The license server must use HTTPS")
        self.public_keys = parse_public_keys(public_keys)
        self.timeout = max(1.0, float(timeout))
        self._ssl_context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context),
            _NoRedirectHandler(),
        )

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/api/v1/licenses/{endpoint}/",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "AI-Voice-Studio/1",
                "X-Nonce": nonce,
                "X-Idempotency-Key": request_id,
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(1_048_576)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise LicenseConfigurationError(
                    "The license server must not redirect requests"
                ) from exc
            if exc.code in {408, 425, 429, 500, 502, 503, 504}:
                raise LicenseNetworkError(
                    "The license server is temporarily unavailable",
                    code="license_service_unavailable",
                    status_code=exc.code,
                ) from exc
            try:
                error_body = json.loads(exc.read(4096).decode("utf-8"))
                error = (
                    error_body.get("error", {}) if isinstance(error_body, dict) else {}
                )
                if not isinstance(error, dict):
                    error = {}
                message = str(
                    error.get("message") or "The license server rejected the request"
                )
                code = str(error.get("code") or "license_server_error")
            except (OSError, ValueError, UnicodeError, http.client.HTTPException):
                message = "The license server rejected the request"
                code = "license_server_error"
            raise LicenseServerError(message, code) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            raise LicenseNetworkError(
                "The license server could not be reached"
            ) from exc
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise LicenseServerError(
                "The license server returned invalid data"
            ) from exc
        if not isinstance(envelope, dict):
            raise LicenseServerError("The license server returned invalid data")
        return envelope

    def activate(
        self,
        license_key: str,
        device_id: str,
        metadata: dict[str, Any] | None = None,
        expected_license_key_id: str | None = None,
        expected_activation_id: str | None = None,
    ) -> dict[str, Any]:
        envelope = self._post(
            "activate",
            {
                "license_key": license_key,
                "device_id": device_id,
                "metadata": metadata or {},
            },
        )
        payload = verify_lease(
            envelope,
            self.public_keys,
            device_id,
            expected_license_key_id=expected_license_key_id,
            expected_activation_id=expected_activation_id,
        )
        return {
            "envelope": envelope,
            "payload": payload,
            "_server_time": payload["_server_time"],
        }

    def validate(
        self,
        license_key: str,
        device_id: str,
        last_server_time: str | None = None,
        expected_license_key_id: str | None = None,
        expected_activation_id: str | None = None,
    ) -> dict[str, Any]:
        envelope = self._post(
            "status",
            {
                "license_key": license_key,
                "device_id": device_id,
                "metadata": {},
            },
        )
        payload = verify_lease(
            envelope,
            self.public_keys,
            device_id,
            last_server_time=(
                parse_time(last_server_time) if last_server_time else None
            ),
            expected_license_key_id=expected_license_key_id,
            expected_activation_id=expected_activation_id,
            require_newer_server_time=True,
        )
        return {
            "envelope": envelope,
            "payload": payload,
            "_server_time": payload["_server_time"],
        }
