import base64
import io
import json
import sys
import types
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.licensing.client import LicenseHttpClient
from app.licensing.crypto import canonical_json, device_binding, verify_lease
from app.licensing.errors import (
    LicenseConfigurationError,
    LicenseExpiredError,
    LicenseNetworkError,
    LicenseServerError,
    LicenseStoreError,
)
from app.licensing.manager import LicenseManager
from app.licensing.store import EncryptedLicenseStore, _default_key_provider


class LicensingTests(unittest.TestCase):
    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_keys = {"test-v1": public_bytes}
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def envelope(
        self,
        device_id="device-a",
        state="active",
        expires_offset=3600,
        grace_offset=3900,
        server_offset=0,
    ):
        payload = {
            "activation_id": "activation-1",
            "client_metadata": {},
            "device_binding": device_binding(device_id),
            "entitlements": {},
            "expires_at": (self.now + timedelta(seconds=expires_offset)).isoformat(),
            "grace_expires_at": (
                self.now + timedelta(seconds=grace_offset)
            ).isoformat(),
            "grace_duration_seconds": grace_offset - expires_offset,
            "issued_at": self.now.isoformat(),
            "lease_duration_seconds": max(expires_offset, 0),
            "lease_id": "lease-1",
            "license_key_id": "license-1",
            "server_time": (self.now + timedelta(seconds=server_offset)).isoformat(),
            "state": state,
            "type": "license_lease",
            "valid": True,
            "version": 1,
        }
        signature = self.private_key.sign(canonical_json(payload).encode("utf-8"))
        return {
            "algorithm": "Ed25519",
            "encoding": "canonical-json",
            "key_id": "test-v1",
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .decode("ascii")
            .rstrip("="),
        }

    def test_lease_is_bound_to_device(self):
        envelope = self.envelope()
        verified = verify_lease(envelope, self.public_keys, "device-a", now=self.now)
        self.assertEqual(verified["activation_id"], "activation-1")
        with self.assertRaises(ValueError):
            verify_lease(envelope, self.public_keys, "device-b", now=self.now)

    def test_expired_lease_can_be_soft_verified_for_refresh(self):
        envelope = self.envelope(expires_offset=-1, grace_offset=120)
        with self.assertRaises(LicenseExpiredError):
            verify_lease(envelope, self.public_keys, "device-a", now=self.now)
        verified = verify_lease(
            envelope,
            self.public_keys,
            "device-a",
            now=self.now,
            allow_expired=True,
        )
        self.assertEqual(verified["state"], "active")

    def test_first_keyring_write_is_accepted(self):
        values = {}

        def get_password(service, account):
            return values.get((service, account))

        def set_password(service, account, value):
            values[(service, account)] = value

        fake_keyring = types.SimpleNamespace(
            get_password=get_password,
            set_password=set_password,
        )
        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            first = _default_key_provider()
            second = _default_key_provider()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_encrypted_store_round_trip_and_tamper_detection(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/license.dat"
            store = EncryptedLicenseStore(path, key_provider=lambda: b"k" * 32)
            state = {"version": 1, "license_key": "LIC-1", "device_id": "device-a"}
            store.save(state)
            self.assertEqual(store.load(), state)
            raw = bytearray(store.path.read_bytes())
            raw[-1] ^= 1
            store.path.write_bytes(raw)
            with self.assertRaises(LicenseStoreError):
                store.load()

    def test_lease_identity_must_match_existing_license(self):
        envelope = self.envelope()
        with self.assertRaises(ValueError):
            verify_lease(
                envelope,
                self.public_keys,
                "device-a",
                now=self.now,
                expected_license_key_id="license-2",
            )

    def test_refresh_rejects_a_replayed_server_timestamp(self):
        envelope = self.envelope()
        with self.assertRaises(ValueError):
            verify_lease(
                envelope,
                self.public_keys,
                "device-a",
                now=self.now,
                last_server_time=self.now,
                require_newer_server_time=True,
            )

    def test_redirects_are_rejected(self):
        client = LicenseHttpClient("https://license.example.test", self.public_keys)
        response = urllib.error.HTTPError(
            "https://license.example.test/api/v1/licenses/activate/",
            307,
            "Temporary Redirect",
            {},
            io.BytesIO(b"{}"),
        )
        with patch.object(client._opener, "open", side_effect=response):
            with self.assertRaisesRegex(LicenseConfigurationError, "must not redirect"):
                client._post("activate", {})

    def test_transient_http_failure_is_retryable(self):
        client = LicenseHttpClient("https://license.example.test", self.public_keys)
        response = urllib.error.HTTPError(
            "https://license.example.test/api/v1/licenses/status/",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"{}"),
        )
        with patch.object(client._opener, "open", side_effect=response):
            with self.assertRaises(LicenseNetworkError) as context:
                client._post("status", {})
        self.assertEqual(context.exception.status_code, 503)

    def test_malformed_error_body_is_reported_cleanly(self):
        client = LicenseHttpClient("https://license.example.test", self.public_keys)
        response = urllib.error.HTTPError(
            "https://license.example.test/api/v1/licenses/activate/",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"error": "invalid"}).encode("utf-8")),
        )
        with patch.object(client._opener, "open", side_effect=response):
            with self.assertRaises(LicenseServerError):
                client._post("activate", {})

    def test_manager_persists_activation_identity_and_refresh_result(self):
        with TemporaryDirectory() as directory:
            store = EncryptedLicenseStore(
                f"{directory}/license.dat", key_provider=lambda: b"k" * 32
            )
            first = self.envelope()
            second = self.envelope(server_offset=1)
            client = types.SimpleNamespace(
                public_keys=self.public_keys,
                activate=lambda *_args, **_kwargs: {
                    "envelope": first,
                    "payload": first["payload"],
                    "_server_time": first["payload"]["server_time"],
                },
                validate=lambda *_args, **_kwargs: {
                    "envelope": second,
                    "payload": second["payload"],
                    "_server_time": second["payload"]["server_time"],
                },
            )
            manager = LicenseManager(
                client, store, device_id="device-a", now=lambda: self.now
            )
            activated = manager.activate("LIC-1", metadata={"custom": "value"})
            self.assertEqual(activated["license_key_id"], "license-1")
            refreshed = manager.refresh()
            self.assertEqual(
                refreshed["last_server_time"], second["payload"]["server_time"]
            )
            self.assertEqual(store.load()["envelope"], second)

    def test_offline_refresh_accepts_unexpired_grace_lease(self):
        with TemporaryDirectory() as directory:
            store = EncryptedLicenseStore(
                f"{directory}/license.dat", key_provider=lambda: b"k" * 32
            )
            state = {
                "version": 1,
                "license_key": "LIC-1",
                "device_id": "device-a",
                "envelope": self.envelope(expires_offset=-1, grace_offset=120),
                "last_server_time": self.now.isoformat(),
            }
            store.save(state)
            client = types.SimpleNamespace(
                public_keys=self.public_keys,
                validate=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    LicenseNetworkError("offline")
                ),
            )
            manager = LicenseManager(
                client,
                store,
                device_id="device-a",
                now=lambda: self.now,
            )
            self.assertEqual(manager.refresh(), state)


if __name__ == "__main__":
    unittest.main()
