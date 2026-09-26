from django.urls import path

from .views import ActivationView, HealthView, StatusView, ValidateView

urlpatterns = [
    path("health/", HealthView.as_view(), name="health"),
    path("api/v1/health/", HealthView.as_view(), name="api-health"),
    path(
        "api/v1/licenses/activate/", ActivationView.as_view(), name="license-activate"
    ),
    path("api/v1/activate/", ActivationView.as_view(), name="activate"),
    path("api/v1/licenses/status/", StatusView.as_view(), name="license-status"),
    path("api/v1/licenses/validate/", ValidateView.as_view(), name="license-validate"),
    path("api/v1/status/", StatusView.as_view(), name="status"),
]
