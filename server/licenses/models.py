import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models
from django.utils import timezone

from .crypto import (
    generate_license_key,
    hash_license_key,
    license_key_hint,
    normalize_license_key,
    validate_license_key_strength,
)


class LicenseKey(models.Model):
    ACTIVE = "active"
    REVOKED = "revoked"
    STATUS_CHOICES = ((ACTIVE, "Active"), (REVOKED, "Revoked"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    key_hint = models.CharField(max_length=16, editable=False)
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_license_keys",
    )

    class Meta:
        ordering = ("-created_at",)
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("status",), name="lic_key_status_idx")
        ]

    def __str__(self) -> str:
        return f"{self.key_hint} ({self.id})"

    @property
    def key_id(self):
        return self.id

    @property
    def is_revoked(self) -> bool:
        return self.status == self.REVOKED

    @classmethod
    def issue(
        cls,
        raw_key: str | None = None,
        actor=None,
        metadata: dict | None = None,
    ) -> tuple["LicenseKey", str]:
        issued_key = raw_key or generate_license_key()
        normalized = (
            validate_license_key_strength(issued_key)
            if raw_key
            else normalize_license_key(issued_key)
        )
        key_hash = hash_license_key(normalized)
        if cls.objects.filter(key_hash=key_hash).exists():
            raise ValueError("License key already exists")
        instance = cls.objects.create(
            key_hash=key_hash,
            key_hint=license_key_hint(normalized),
            metadata=metadata or {},
            created_by=actor,
        )
        return instance, normalized

    def revoke(self, actor=None, reason: str = "administrative revocation") -> bool:
        if self.status == self.REVOKED:
            return False
        self.status = self.REVOKED
        self.revoked_at = timezone.now()
        self.revocation_reason = reason[:2000]
        self.save(
            update_fields=("status", "revoked_at", "revocation_reason", "updated_at")
        )
        return True


class DeviceActivation(models.Model):
    ACTIVE = "active"
    REVOKED = "revoked"
    STATUS_CHOICES = ((ACTIVE, "Active"), (REVOKED, "Revoked"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license_key = models.OneToOneField(
        LicenseKey,
        on_delete=models.PROTECT,
        related_name="activation",
    )
    device_id_hash = models.CharField(max_length=64)
    device_id_hint = models.CharField(max_length=16)
    metadata = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=ACTIVE)
    activated_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=("license_key", "device_id_hash"),
                name="lic_act_license_device_uniq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("status",), name="lic_dev_status_idx"),
            models.Index(
                fields=("license_key", "last_seen_at"),
                name="lic_dev_license_seen_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.device_id_hint} ({self.id})"

    @property
    def activation_id(self):
        return self.id


class LicenseLease(models.Model):
    ACTIVE = "active"
    GRACE = "grace"
    STATE_CHOICES = ((ACTIVE, "Active"), (GRACE, "Grace"))

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    license_key = models.ForeignKey(
        LicenseKey,
        on_delete=models.PROTECT,
        related_name="leases",
    )
    device_activation = models.ForeignKey(
        DeviceActivation,
        on_delete=models.PROTECT,
        related_name="leases",
    )
    issued_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    grace_expires_at = models.DateTimeField()
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=ACTIVE)
    signing_key_id = models.CharField(max_length=64)
    canonical_payload = models.TextField()
    signature = models.CharField(max_length=128)
    payload = models.JSONField()

    class Meta:
        ordering = ("-issued_at",)
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("device_activation", "-issued_at"),
                name="lic_lease_device_idx",
            ),
            models.Index(
                fields=("license_key", "-issued_at"),
                name="lic_lease_license_idx",
            ),
        ]

    def __str__(self) -> str:
        return str(self.id)

    @property
    def lease_id(self):
        return self.id

    def state_at(self, moment=None) -> str | None:
        moment = moment or timezone.now()
        if moment <= self.expires_at:
            return self.ACTIVE
        if moment <= self.grace_expires_at:
            return self.GRACE
        return None


class AuditEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    event_type = models.CharField(max_length=64, db_index=True)
    license_key = models.ForeignKey(
        LicenseKey,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    device_activation = models.ForeignKey(
        DeviceActivation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="license_audit_events",
    )
    nonce_hash = models.CharField(max_length=64, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("event_type", "-created_at"),
                name="lic_audit_event_idx",
            ),
            models.Index(
                fields=("license_key", "-created_at"),
                name="lic_audit_license_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.id})"


class RequestNonce(models.Model):
    id = models.BigAutoField(primary_key=True)
    endpoint = models.CharField(max_length=128)
    nonce_hash = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=("endpoint", "nonce_hash"),
                name="lic_nonce_scope_uniq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("expires_at",), name="lic_nonce_expiry_idx")
        ]

    def __str__(self) -> str:
        return f"{self.endpoint}:{self.nonce_hash[:12]}"


class IdempotencyRecord(models.Model):
    id = models.BigAutoField(primary_key=True)
    endpoint = models.CharField(max_length=128)
    idempotency_key_hash = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)
    response_status = models.PositiveSmallIntegerField()
    response_body = models.TextField()
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=("endpoint", "idempotency_key_hash"),
                name="lic_idem_scope_key_uniq",
            )
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=("expires_at",), name="lic_idem_expiry_idx")
        ]

    def __str__(self) -> str:
        return f"{self.endpoint}:{self.idempotency_key_hash[:12]}"
