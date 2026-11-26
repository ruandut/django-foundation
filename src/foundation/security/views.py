"""Views and a mixin for the sign-in routes: the captcha plus the strict per-IP "auth" limit."""

from rest_framework_simplejwt.views import TokenObtainPairView

from . import captcha
from .throttling import AuthThrottle, with_defaults


class CaptchaProtected:
    """For a DRF view whose POST signs someone in, or is otherwise worth guessing at: limits it
    per IP and needs a solved captcha (`altcha` in the body). The captcha is checked after
    authentication, permissions and throttles, and before the view's own work, so every guess
    spends a challenge."""

    throttle_classes = with_defaults(AuthThrottle)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method == "POST":
            captcha.verify(request)


class LoginView(CaptchaProtected, TokenObtainPairView):
    """SimpleJWT's token endpoint behind the captcha. Subclass it to set serializer_class."""
