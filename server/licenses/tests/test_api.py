import base64
import hashlib
import json
from datetime import timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from licenses.crypto import (
    canonical_json,
    hash_device_id,
    hash_license_key,
    hmac_pepper,
    public_device_binding,
    public_key_bytes,
    sign_payload,
    verify_signed_response,
)
from licenses.models import (
    AuditEvent,
    DeviceActivation,
    LicenseKey,
    LicenseLease,
    RequestNonce,
)
from licenses.services import request_fingerprint

_PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
_PRIVATE_KEY_V2 = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
_PRIVATE_MATERIAL = (
    base64.urlsafe_b64encode(
        _PRIVATE_KEY.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    .decode("ascii")
    .rstrip("=")
)
_PRIVATE_MATERIAL_V2 = (
    base64.urlsafe_b64encode(
        _PRIVATE_KEY_V2.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    .decode("ascii")
    .rstrip("=")
)
TEST_SETTINGS = {
    "LICENSE_HMAC_PEPPER": "test-hmac-pepper-0123456789abcdef-0123456789",
    "LICENSE_SIGNING_KEYS": {
        "test-v1": _PRIVATE_MATERIAL,
        "test-v2": _PRIVATE_MATERIAL_V2,
    },
    "LICENSE_SIGNING_KEY_ID": "test-v1",
    "LICENSE_LEASE_DURATION_SECONDS": 3600,
    "LICENSE_LEASE_GRACE_SECONDS": 300,
    "LICENSE_NONCE_TTL_SECONDS": 900,
    "LICENSE_IDEMPOTENCY_TTL_SECONDS": 86400,
}


@override_settings(**TEST_SETTINGS)
class LicenseAPITests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.license, self.raw_key = LicenseKey.issue(
            "LIC-TEST-ABCDE-23456-FGHIJ-KLMNO"
        )

    def post(
        self,
        path,
        device_id="device-a",
        nonce="nonce-000000000001",
        idempotency_key="idem-000000000001",
        **extra,
    ):
        data = {
            "license_key": self.raw_key,
            "device_id": device_id,
            "nonce": nonce,
            "idempotency_key": idempotency_key,
            **extra,
        }
        return self.client.post(
            path,
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_activation_binds_device_and_returns_verifiable_lease(self):
        response = self.post(reverse("license-activate"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(
            verify_signed_response(
                body,
                {"test-v1": public_key_bytes("test-v1")[1]},
            )
        )
        self.assertEqual(body["key_id"], "test-v1")
        self.assertEqual(body["payload"]["state"], "active")
        self.assertEqual(
            body["payload"]["device_binding"], public_device_binding("device-a")
        )
        self.assertNotIn(self.raw_key, response.content.decode())
        self.assertEqual(DeviceActivation.objects.count(), 1)
        self.assertEqual(LicenseLease.objects.count(), 1)
        self.assertEqual(
            self.license.key_hash,
            hash_license_key(self.raw_key),
        )
        self.assertEqual(
            LicenseLease.objects.get().canonical_payload,
            canonical_json(body["payload"]),
        )

    def test_idempotent_retry_returns_the_original_response(self):
        first = self.post(reverse("license-activate"))
        second = self.post(reverse("license-activate"))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, first.status_code)
        self.assertEqual(second.content, first.content)
        self.assertEqual(LicenseLease.objects.count(), 1)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="activation_created").count(), 1
        )

    def test_nonce_replay_is_rejected_without_a_second_lease(self):
        first = self.post(
            reverse("license-activate"),
            nonce="same-nonce",
            idempotency_key="first-idempotency",
        )
        replay = self.post(
            reverse("license-activate"),
            nonce="same-nonce",
            idempotency_key="second-idempotency",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["error"]["code"], "replay_detected")
        self.assertEqual(LicenseLease.objects.count(), 1)

    def test_a_license_cannot_be_bound_to_a_second_device(self):
        self.assertEqual(
            self.post(reverse("license-activate"), device_id="device-a").status_code,
            200,
        )
        response = self.post(
            reverse("license-activate"),
            device_id="device-b",
            nonce="nonce-000000000002",
            idempotency_key="idem-000000000002",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "device_binding_rejected")
        self.assertEqual(
            DeviceActivation.objects.get().device_id_hash, hash_device_id("device-a")
        )
        self.assertEqual(DeviceActivation.objects.count(), 1)

    def test_status_validation_rejects_a_different_device(self):
        self.assertEqual(
            self.post(reverse("license-activate"), device_id="device-a").status_code,
            200,
        )
        response = self.post(
            reverse("license-status"),
            device_id="device-b",
            nonce="status-wrong-device-nonce",
            idempotency_key="status-wrong-device-idem",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "device_binding_rejected")
        self.assertEqual(LicenseLease.objects.count(), 1)

    def test_request_fingerprints_are_keyed_and_do_not_store_raw_keys(self):
        self.assertEqual(self.post(reverse("license-activate")).status_code, 200)
        payload = {
            "license_key": self.raw_key,
            "device_id": "device-a",
            "metadata": {},
            "nonce": "nonce-000000000001",
            "idempotency_key": "idem-000000000001",
        }
        stored = RequestNonce.objects.get().request_fingerprint
        semantic = {
            key: value
            for key, value in payload.items()
            if key not in {"nonce", "idempotency_key"}
        }
        legacy = hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
        self.assertEqual(stored, request_fingerprint(payload))
        self.assertNotEqual(stored, legacy)
        self.assertNotIn(self.raw_key, stored)

    def test_custom_license_keys_require_minimum_strength(self):
        with self.assertRaises(ValueError):
            LicenseKey.issue("SHORT")

    @override_settings(DEBUG=False, LICENSE_HMAC_PEPPER="replace-with-a-secret")
    def test_production_rejects_placeholder_hmac_pepper(self):
        with self.assertRaises(ImproperlyConfigured):
            hmac_pepper()

    @override_settings(LICENSE_LEASE_RETENTION_COUNT=0)
    def test_zero_lease_retention_fails_closed(self):
        response = self.post(reverse("license-activate"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "service_unavailable",
        )

    @override_settings(LICENSE_NONCE_TTL_SECONDS=0)
    def test_zero_nonce_ttl_fails_closed(self):
        response = self.post(reverse("license-activate"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "service_unavailable",
        )

    def test_lease_history_is_bounded(self):
        self.assertEqual(self.post(reverse("license-activate")).status_code, 200)
        for index in range(12):
            response = self.post(
                reverse("license-status"),
                nonce=f"retention-nonce-{index}",
                idempotency_key=f"retention-idem-{index}",
            )
            self.assertEqual(response.status_code, 200)
        self.assertLessEqual(LicenseLease.objects.count(), 10)

    def test_status_validation_supports_grace_then_rejects_expired_leases(self):
        activation = self.post(reverse("license-activate"))
        self.assertEqual(activation.status_code, 200)
        status = self.post(
            reverse("license-status"),
            nonce="status-nonce-000001",
            idempotency_key="status-idem-000001",
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["payload"]["state"], "active")
        lease = LicenseLease.objects.order_by("-issued_at").first()
        lease.expires_at = timezone.now() - timedelta(seconds=1)
        lease.grace_expires_at = timezone.now() + timedelta(seconds=120)
        lease.save(update_fields=("expires_at", "grace_expires_at"))
        grace = self.post(
            reverse("license-status"),
            nonce="status-nonce-000002",
            idempotency_key="status-idem-000002",
        )
        self.assertEqual(grace.status_code, 200)
        self.assertEqual(grace.json()["payload"]["state"], "grace")
        lease.refresh_from_db()
        lease.grace_expires_at = timezone.now() - timedelta(seconds=1)
        lease.save(update_fields=("grace_expires_at",))
        expired = self.post(
            reverse("license-status"),
            nonce="status-nonce-000003",
            idempotency_key="status-idem-000003",
        )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.json()["error"]["code"], "invalid_license")

    def test_revocation_blocks_status_validation(self):
        self.assertEqual(self.post(reverse("license-activate")).status_code, 200)
        self.license.revoke(reason="customer request")
        response = self.post(
            reverse("license-status"),
            nonce="revoked-status-nonce",
            idempotency_key="revoked-status-idem",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_license")

    def test_idempotency_key_cannot_be_reused_for_different_input(self):
        first = self.post(reverse("license-activate"), device_id="device-a")
        conflict = self.post(
            reverse("license-activate"),
            device_id="device-b",
            nonce="different-nonce",
            idempotency_key="idem-000000000001",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")

    def test_request_headers_can_supply_replay_fields(self):
        response = self.client.post(
            reverse("license-activate"),
            data=json.dumps({"license_key": self.raw_key, "device_id": "device-a"}),
            content_type="application/json",
            HTTP_X_NONCE="header-nonce-000001",
            HTTP_X_IDEMPOTENCY_KEY="header-idem-000001",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["payload"]["state"], "active")

    def test_health_endpoint_is_public(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_public_endpoints_do_not_offer_creation_or_revocation(self):
        self.assertEqual(self.client.get(reverse("license-activate")).status_code, 405)
        self.assertEqual(
            self.client.delete(reverse("license-activate")).status_code, 405
        )
        self.assertEqual(LicenseKey.objects.count(), 1)


@override_settings(**TEST_SETTINGS)
class AdminWorkflowTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(
            get_user_model().objects.create_superuser(
                username="admin",
                email="admin@example.com",
                password="test-password-123",
            )
        )

    def test_admin_rejects_weak_custom_license_keys(self):
        response = self.client.post(
            reverse("admin:licenses_licensekey_add"),
            {"metadata": "{}", "raw_key": "SHORT", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LicenseKey.objects.count(), 0)

    def test_admin_can_issue_and_revoke_license_keys(self):
        response = self.client.post(
            reverse("admin:licenses_licensekey_add"),
            {
                "metadata": "{}",
                "raw_key": "LIC-ADMIN-TEST-KEY-12345",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        license_key = LicenseKey.objects.get()
        self.assertEqual(license_key.status, LicenseKey.ACTIVE)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="license_created").count(), 1
        )

        response = self.client.post(
            reverse("admin:licenses_licensekey_changelist"),
            {
                "action": "revoke_selected",
                "_selected_action": [str(license_key.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        license_key.refresh_from_db()
        self.assertEqual(license_key.status, LicenseKey.REVOKED)
        self.assertEqual(
            AuditEvent.objects.filter(event_type="license_revoked").count(), 1
        )


@override_settings(**TEST_SETTINGS)
class SigningTests(TestCase):
    def test_rotation_uses_the_configured_active_key_id(self):
        with self.settings(LICENSE_SIGNING_KEY_ID="test-v2"):
            envelope = sign_payload({"type": "rotation_test"}, "test-v2")

        self.assertEqual(envelope["key_id"], "test-v2")
        self.assertTrue(
            verify_signed_response(
                envelope,
                {"test-v2": public_key_bytes("test-v2")[1]},
            )
        )

    def test_canonical_json_is_stable_and_signature_verifies(self):
        payload = {"z": 1, "a": {"b": 2, "a": 1}}
        envelope = sign_payload(payload, "test-v1")
        self.assertEqual(canonical_json(payload), '{"a":{"a":1,"b":2},"z":1}')
        self.assertTrue(
            verify_signed_response(
                envelope,
                {"test-v1": public_key_bytes("test-v1")[1]},
            )
        )
        envelope["payload"]["z"] = 2
        self.assertFalse(
            verify_signed_response(
                envelope,
                {"test-v1": public_key_bytes("test-v1")[1]},
            )
        )
