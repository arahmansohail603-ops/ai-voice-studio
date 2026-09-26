from rest_framework import serializers

from .crypto import canonical_json, normalize_device_id, normalize_license_key


class ClientRequestSerializer(serializers.Serializer):
    license_key = serializers.CharField(
        max_length=512, trim_whitespace=False, write_only=True
    )
    device_id = serializers.CharField(
        max_length=512, trim_whitespace=True, write_only=True
    )
    nonce = serializers.CharField(max_length=128, trim_whitespace=True, write_only=True)
    idempotency_key = serializers.CharField(
        max_length=128,
        trim_whitespace=True,
        write_only=True,
    )
    metadata = serializers.JSONField(required=False, default=dict, write_only=True)

    def validate_license_key(self, value: str) -> str:
        try:
            return normalize_license_key(value)
        except ValueError as exc:
            raise serializers.ValidationError("Invalid license key.") from exc

    def validate_device_id(self, value: str) -> str:
        try:
            return normalize_device_id(value)
        except ValueError as exc:
            raise serializers.ValidationError("Invalid device identifier.") from exc

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("Metadata must be an object.")
        from django.conf import settings

        if (
            len(canonical_json(value).encode("utf-8"))
            > settings.LICENSE_MAX_METADATA_BYTES
        ):
            raise serializers.ValidationError("Metadata is too large.")
        return value


class ActivationRequestSerializer(ClientRequestSerializer):
    pass


class StatusRequestSerializer(ClientRequestSerializer):
    pass
