from django import forms
from django.contrib import admin, messages
from django.db import IntegrityError
from django.utils.html import format_html

from .crypto import (
    generate_license_key,
    hash_license_key,
    license_key_hint,
    normalize_license_key,
    validate_license_key_strength,
)
from .models import AuditEvent, DeviceActivation, LicenseKey, LicenseLease
from .services import record_audit


class LicenseKeyForm(forms.ModelForm):
    raw_key = forms.CharField(
        required=False,
        label="Raw license key",
        help_text=(
            "Leave blank to generate a cryptographically random key. "
            "It is shown only after creation."
        ),
        max_length=512,
    )

    class Meta:
        model = LicenseKey
        fields = ("metadata",)

    def clean_raw_key(self):
        value = self.cleaned_data.get("raw_key", "").strip()
        if value:
            try:
                return validate_license_key_strength(value)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
        return value

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance._state.adding:
            self.fields["raw_key"].disabled = True
            self.fields["raw_key"].required = False


@admin.register(LicenseKey)
class LicenseKeyAdmin(admin.ModelAdmin):
    form = LicenseKeyForm
    list_display = (
        "key_hint",
        "id",
        "status",
        "created_at",
        "revoked_at",
        "created_by",
    )
    list_filter = ("status", "created_at", "revoked_at")
    search_fields = ("key_hint", "id", "key_hash")
    readonly_fields = (
        "id",
        "key_hash",
        "key_hint",
        "status",
        "created_at",
        "updated_at",
        "revoked_at",
        "revocation_reason",
        "created_by",
    )
    actions = ("revoke_selected",)
    ordering = ("-created_at",)

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        if change:
            obj.save()
            return
        raw_key = form.cleaned_data.get("raw_key") or generate_license_key()
        try:
            normalized = (
                validate_license_key_strength(raw_key)
                if form.cleaned_data.get("raw_key")
                else normalize_license_key(raw_key)
            )
            key_hash = hash_license_key(normalized)
        except ValueError as exc:
            raise forms.ValidationError("Enter a valid raw license key.") from exc
        obj.key_hash = key_hash
        obj.key_hint = license_key_hint(normalized)
        obj.status = LicenseKey.ACTIVE
        obj.created_by = request.user
        try:
            obj.save()
        except IntegrityError as exc:
            raise forms.ValidationError("That license key already exists.") from exc
        record_audit("license_created", license_key=obj, actor=request.user)
        request.issued_license_raw_key = normalized

    def response_add(self, request, obj, post_url_continue=None):
        issued_key = getattr(request, "issued_license_raw_key", "")
        if issued_key:
            messages.success(
                request,
                format_html("License key issued: <code>{}</code>", issued_key),
            )
        return super().response_add(request, obj, post_url_continue)

    @admin.action(description="Revoke selected license keys")
    def revoke_selected(self, request, queryset):
        revoked = 0
        for license_key in queryset:
            if license_key.revoke(actor=request.user):
                revoked += 1
                record_audit(
                    "license_revoked",
                    license_key=license_key,
                    actor=request.user,
                    metadata={"reason": license_key.revocation_reason},
                )
        self.message_user(
            request,
            f"{revoked} license key{'s' if revoked != 1 else ''} revoked.",
            messages.INFO,
        )


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DeviceActivation)
class DeviceActivationAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "license_key",
        "device_id_hint",
        "status",
        "activated_at",
        "last_seen_at",
    )
    list_filter = ("status", "activated_at", "last_seen_at")
    search_fields = ("id", "device_id_hash", "license_key__id", "license_key__key_hint")
    readonly_fields = (
        "id",
        "license_key",
        "device_id_hash",
        "device_id_hint",
        "metadata",
        "status",
        "activated_at",
        "last_seen_at",
        "revoked_at",
    )


@admin.register(LicenseLease)
class LicenseLeaseAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "license_key",
        "device_activation",
        "state",
        "issued_at",
        "expires_at",
    )
    list_filter = ("state", "issued_at", "expires_at")
    search_fields = ("id", "license_key__id", "device_activation__id", "signing_key_id")
    readonly_fields = (
        "id",
        "license_key",
        "device_activation",
        "issued_at",
        "expires_at",
        "grace_expires_at",
        "state",
        "signing_key_id",
        "canonical_payload",
        "signature",
        "payload",
    )


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = (
        "event_type",
        "license_key",
        "device_activation",
        "actor",
        "created_at",
    )
    list_filter = ("event_type", "created_at")
    search_fields = (
        "event_type",
        "id",
        "license_key__id",
        "device_activation__id",
        "request_fingerprint",
    )
    readonly_fields = (
        "id",
        "event_type",
        "license_key",
        "device_activation",
        "actor",
        "nonce_hash",
        "request_fingerprint",
        "ip_address",
        "user_agent",
        "metadata",
        "created_at",
    )
