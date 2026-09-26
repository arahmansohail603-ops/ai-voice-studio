import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    initial = True

    dependencies: ClassVar[list] = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list] = [
        migrations.CreateModel(
            name="LicenseKey",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "key_hash",
                    models.CharField(editable=False, max_length=64, unique=True),
                ),
                ("key_hint", models.CharField(editable=False, max_length=16)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("revoked", "Revoked")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("revocation_reason", models.TextField(blank=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_license_keys",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="DeviceActivation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("device_id_hash", models.CharField(max_length=64)),
                ("device_id_hint", models.CharField(max_length=16)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("revoked", "Revoked")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("activated_at", models.DateTimeField(auto_now_add=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "license_key",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="activation",
                        to="licenses.licensekey",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="LicenseLease",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("issued_at", models.DateTimeField(default=timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("grace_expires_at", models.DateTimeField()),
                (
                    "state",
                    models.CharField(
                        choices=[("active", "Active"), ("grace", "Grace")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("signing_key_id", models.CharField(max_length=64)),
                ("canonical_payload", models.TextField()),
                ("signature", models.CharField(max_length=128)),
                ("payload", models.JSONField()),
                (
                    "device_activation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="leases",
                        to="licenses.deviceactivation",
                    ),
                ),
                (
                    "license_key",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="leases",
                        to="licenses.licensekey",
                    ),
                ),
            ],
            options={"ordering": ("-issued_at",)},
        ),
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                (
                    "id",
                    models.BigAutoField(primary_key=True, serialize=False),
                ),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("nonce_hash", models.CharField(blank=True, max_length=64)),
                ("request_fingerprint", models.CharField(blank=True, max_length=64)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=512)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="license_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "device_activation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_events",
                        to="licenses.deviceactivation",
                    ),
                ),
                (
                    "license_key",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="audit_events",
                        to="licenses.licensekey",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="RequestNonce",
            fields=[
                (
                    "id",
                    models.BigAutoField(primary_key=True, serialize=False),
                ),
                ("endpoint", models.CharField(max_length=128)),
                ("nonce_hash", models.CharField(max_length=64)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="IdempotencyRecord",
            fields=[
                (
                    "id",
                    models.BigAutoField(primary_key=True, serialize=False),
                ),
                ("endpoint", models.CharField(max_length=128)),
                ("idempotency_key_hash", models.CharField(max_length=64)),
                ("request_fingerprint", models.CharField(max_length=64)),
                ("response_status", models.PositiveSmallIntegerField()),
                ("response_body", models.TextField()),
                ("expires_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddIndex(
            model_name="licensekey",
            index=models.Index(fields=["status"], name="lic_key_status_idx"),
        ),
        migrations.AddIndex(
            model_name="deviceactivation",
            index=models.Index(fields=["status"], name="lic_dev_status_idx"),
        ),
        migrations.AddIndex(
            model_name="deviceactivation",
            index=models.Index(
                fields=["license_key", "last_seen_at"],
                name="lic_dev_license_seen_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="licenselease",
            index=models.Index(
                fields=["device_activation", "-issued_at"],
                name="lic_lease_device_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="licenselease",
            index=models.Index(
                fields=["license_key", "-issued_at"],
                name="lic_lease_license_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["event_type", "-created_at"], name="lic_audit_event_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="auditevent",
            index=models.Index(
                fields=["license_key", "-created_at"],
                name="lic_audit_license_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="requestnonce",
            index=models.Index(fields=["expires_at"], name="lic_nonce_expiry_idx"),
        ),
        migrations.AddIndex(
            model_name="idempotencyrecord",
            index=models.Index(fields=["expires_at"], name="lic_idem_expiry_idx"),
        ),
        migrations.AddConstraint(
            model_name="deviceactivation",
            constraint=models.UniqueConstraint(
                fields=("license_key", "device_id_hash"),
                name="lic_act_license_device_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="requestnonce",
            constraint=models.UniqueConstraint(
                fields=("endpoint", "nonce_hash"),
                name="lic_nonce_scope_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="idempotencyrecord",
            constraint=models.UniqueConstraint(
                fields=("endpoint", "idempotency_key_hash"),
                name="lic_idem_scope_key_uniq",
            ),
        ),
    ]
