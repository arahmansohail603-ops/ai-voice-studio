from rest_framework.throttling import ScopedRateThrottle


class ActivationRateThrottle(ScopedRateThrottle):
    scope = "license_activation"


class StatusRateThrottle(ScopedRateThrottle):
    scope = "license_status"


class HealthRateThrottle(ScopedRateThrottle):
    scope = "health"
