"""Views and a mixin for the sign-in routes: the captcha plus the strict per-IP "auth" limit."""

from django.apps import apps
from rest_framework.response import Response
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
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
    """SimpleJWT's token endpoint behind the captcha. Subclass it to set serializer_class.

    With foundation.mfa installed, someone with a second factor gets no tokens here: they get
    {"mfa_required": true, "mfa_token", "methods"} and finish at foundation.mfa's login/."""

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as e:
            raise InvalidToken(e.args[0]) from e
        if apps.is_installed("foundation.mfa"):
            from foundation.mfa import services

            if services.is_enabled(serializer.user):
                # The serializer made tokens already; they are dropped, never sent.
                return Response(services.second_step(serializer.user))
        return Response(serializer.validated_data)
