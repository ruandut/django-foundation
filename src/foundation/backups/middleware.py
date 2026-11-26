from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.utils.html import escape

from . import maintenance


class MaintenanceMiddleware:
    """While a rollback runs, every request gets 503 Service Unavailable with Retry-After.
    The API answers {"code": "maintenance", "detail": ...}, which the frontend turns into its
    maintenance page; anything else (Django admin) gets a plain page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not maintenance.in_progress():
            return self.get_response(request)
        headers = {"Retry-After": str(maintenance.RETRY_AFTER_SECONDS)}
        if request.path.startswith("/api/"):
            body = {"code": maintenance.IN_PROGRESS, "detail": settings.MAINTENANCE_MESSAGE}
            return JsonResponse(body, status=503, headers=headers)
        html = (
            "<!doctype html><title>Down for maintenance</title>"
            f"<h1>Down for maintenance</h1><p>{escape(settings.MAINTENANCE_MESSAGE)}</p>"
        )
        return HttpResponse(html, status=503, headers=headers)
