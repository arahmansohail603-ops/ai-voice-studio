from __future__ import annotations


class LicenseError(Exception):
    code = "license_error"


class LicenseConfigurationError(LicenseError):
    code = "license_configuration_error"


class LicenseStoreError(LicenseError):
    code = "license_store_error"


class LicenseDeviceError(LicenseError):
    code = "license_device_error"


class LicenseNetworkError(LicenseError):
    code = "license_network_error"

    def __init__(
        self,
        message: str,
        code: str = "license_network_error",
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LicenseServerError(LicenseError):
    code = "license_server_error"

    def __init__(self, message: str, code: str = "license_server_error") -> None:
        super().__init__(message)
        self.code = code


class LicenseExpiredError(LicenseError):
    code = "license_expired"
