from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from foundation.security.login import protect_admin_login
from foundation.security.views import LoginView
from tests.views import health

protect_admin_login()

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("foundation.security.urls")),
    path("api/auth/token/", LoginView.as_view()),
    path("api/auth/token/refresh/", TokenRefreshView.as_view()),
    path("api/auth/mfa/", include("foundation.mfa.urls")),
    path("api/health/", health),
    path("api/units/", include("tests.private_urls")),
]
