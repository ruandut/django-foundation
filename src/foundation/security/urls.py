"""Include under the path settings.CAPTCHA_CHALLENGE_URL points at, e.g.
path("api/auth/", include("foundation.security.urls")) for /api/auth/captcha/."""

from django.urls import path

from .captcha import ChallengeView

urlpatterns = [path("captcha/", ChallengeView.as_view(), name="captcha")]
