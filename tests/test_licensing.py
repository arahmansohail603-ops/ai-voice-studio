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
        payload_overrides=None,
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
        if payload_overrides:
            payload.update(payload_overrides)
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


class MalformedLeaseTests(unittest.TestCase):
    """A hostile or buggy server must be rejected, never crash the app.

    ``main.start()`` catches only ``(LicenseError, RuntimeError, ValueError)``.
    A ``TypeError`` or ``OverflowError`` escaping ``verify_lease`` therefore
    propagated out of ``main()`` -- and the shipped build has the console
    disabled, so a customer double-clicking the executable got no window, no
    dialog and no message, just a process that died. Each case below is a real
    way a value like that reaches a signed payload.
    """

    def setUp(self):
        self.private_key = Ed25519PrivateKey.generate()
        public_bytes = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_keys = {"test-v1": public_bytes}
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _verify(self, payload_overrides, **kwargs):
        payload = {
            "activation_id": "activation-1",
            "client_metadata": {},
            "device_binding": device_binding("device-a"),
            "entitlements": {},
            "expires_at": (self.now + timedelta(seconds=3600)).isoformat(),
            "grace_expires_at": (self.now + timedelta(seconds=3900)).isoformat(),
            "grace_duration_seconds": 300,
            "issued_at": self.now.isoformat(),
            "lease_duration_seconds": 3600,
            "lease_id": "lease-1",
            "license_key_id": "license-1",
            "server_time": self.now.isoformat(),
            "state": "active",
            "type": "license_lease",
            "valid": True,
            "version": 1,
        }
        payload.update(payload_overrides)
        signature = self.private_key.sign(canonical_json(payload).encode("utf-8"))
        envelope = {
            "algorithm": "Ed25519",
            "encoding": "canonical-json",
            "key_id": "test-v1",
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .decode("ascii")
            .rstrip("="),
        }
        return verify_lease(
            envelope, self.public_keys, "device-a", now=self.now, **kwargs
        )

    def test_non_ascii_license_key_id_is_rejected(self):
        # hmac.compare_digest raises TypeError on non-ASCII str.
        with self.assertRaises(ValueError):
            self._verify(
                {"license_key_id": "licéns"}, expected_license_key_id="licéns"
            )

    def test_non_ascii_activation_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self._verify({"activation_id": "aé"}, expected_activation_id="aé")

    def test_non_ascii_device_binding_is_rejected(self):
        with self.assertRaises(ValueError):
            self._verify({"device_binding": "dév"})

    def test_out_of_range_utc_offset_is_rejected(self):
        # The offset pushes the instant below datetime.min, and OverflowError
        # is an ArithmeticError, not a ValueError.
        for value in ("0001-01-01T00:00:00+10:00", "9999-12-31T23:59:59-10:00"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self._verify({"expires_at": value})

    def test_unparseable_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            self._verify({"expires_at": "not-a-date"})

    def test_a_slow_local_clock_does_not_lock_out_a_valid_lease(self):
        """A machine running behind must still be able to start the app.

        Every field here is signed, so the response is exactly what the server
        sent; only this machine's clock is wrong. The check that rejected it
        compared a server-issued timestamp against a local clock nobody in the
        app controls, so a slow clock made every response look "dated in the
        future" and the activation dialog could never be satisfied.
        """
        for behind in (6 * 60, 2 * 3600, 24 * 3600):
            with self.subTest(seconds_behind=behind):
                verified = verify_lease(
                    self._envelope_for_clock(),
                    self.public_keys,
                    "device-a",
                    now=self.now - timedelta(seconds=behind),
                )
                self.assertEqual(verified["state"], "active")

    def _envelope_for_clock(self, server_offset=0):
        payload = {
            "activation_id": "activation-1",
            "client_metadata": {},
            "device_binding": device_binding("device-a"),
            "entitlements": {},
            "expires_at": (self.now + timedelta(seconds=3600)).isoformat(),
            "grace_expires_at": (self.now + timedelta(seconds=3900)).isoformat(),
            "grace_duration_seconds": 300,
            "issued_at": self.now.isoformat(),
            "lease_duration_seconds": 3600,
            "lease_id": "lease-1",
            "license_key_id": "license-1",
            "server_time": (self.now + timedelta(seconds=server_offset)).isoformat(),
            "state": "active",
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

    def test_a_slow_clock_still_cannot_extend_an_expired_lease(self):
        """Anchoring on the signed server_time must not become a free extension.

        Here the response's own signed ``server_time`` already sits past the
        lease's grace deadline, so the server is saying the lease is over. A
        local clock rolled back a year must not talk the client out of that:
        before the fix the deadline was compared against the local clock only,
        so a machine running slow accepted a lease the server had ended.
        """
        envelope = self._envelope_for_clock(server_offset=4000)
        with self.assertRaises(LicenseExpiredError):
            verify_lease(
                envelope,
                self.public_keys,
                "device-a",
                now=self.now - timedelta(days=365),
            )


class KeystoreFailureTests(unittest.TestCase):
    """A broken keystore must not be reported as a tampered license store."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.path = f"{self.directory.name}/license.dat"
        EncryptedLicenseStore(self.path, key_provider=lambda: b"k" * 32).save(
            {"version": 1, "license_key": "LIC-1", "device_id": "device-a"}
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_unavailable_keystore_keeps_its_own_message(self):
        def broken():
            raise LicenseConfigurationError(
                "The operating-system keystore is unavailable"
            )

        store = EncryptedLicenseStore(self.path, key_provider=broken)
        with self.assertRaises(LicenseConfigurationError):
            store.load()

    def test_a_short_key_is_a_keystore_problem_not_tampering(self):
        store = EncryptedLicenseStore(self.path, key_provider=lambda: b"too short")
        with self.assertRaises(LicenseConfigurationError):
            store.load()

    def test_load_and_save_agree_when_the_keystore_is_down(self):
        """The two halves of the same store must name the same failure.

        Reporting "missing or has been modified" from load() sent the user to
        delete license.dat, which then made save() fail with the real message
        instead -- an unwinnable loop.
        """

        def broken():
            raise RuntimeError("no keyring backend")

        loaded = EncryptedLicenseStore(self.path, key_provider=broken)
        with self.assertRaises(Exception) as load_context:
            loaded.load()
        with self.assertRaises(Exception) as save_context:
            loaded.save({"version": 1})
        self.assertEqual(
            type(load_context.exception), type(save_context.exception)
        )


if __name__ == "__main__":
    unittest.main()
