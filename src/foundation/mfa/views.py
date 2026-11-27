"""The 2FA API. Signed-in people manage their own factors; the two `login/` routes are the
public second step of signing in, after foundation.security's LoginView. The work is in
services.py; these check input, the password and who is asking."""

from allauth.core import context
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from foundation.security.throttling import AuthThrottle, with_defaults

from . import services
from .services import Refused


class MFAThrottle(UserRateThrottle):
    """Per account, on the changes that check the password."""

    scope = "mfa"


def refused(error: Refused, field: str = "detail"):
    status_code = status.HTTP_403_FORBIDDEN if error.code == "locked" else 400
    if field == "detail":
        return Response({"detail": error.message, "code": error.code}, status=status_code)
    raise ValidationError({field: [error.message]})


class PasswordSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise ValidationError("That isn't your password.")
        return value


class ActivateTOTPSerializer(PasswordSerializer):
    code = serializers.CharField()


class AddKeySerializer(PasswordSerializer):
    name = serializers.CharField(max_length=100)
    credential = serializers.JSONField()


class NameSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class ManageView(APIView):
    """Changes, other than renaming, are limited per account and need the password."""

    def get_throttles(self):
        extra = [] if self.request.method in ("GET", "PATCH") else [MFAThrottle()]
        return [*super().get_throttles(), *extra]

    def checked(self, serializer_class):
        serializer = serializer_class(data=self.request.data, context={"request": self.request})
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data


class OverviewView(APIView):
    def get(self, request):
        return Response(services.overview(request.user))


class TOTPSetupView(ManageView):
    def post(self, request):
        try:
            with context.request_context(request):
                return Response(services.start_totp(request.user))
        except Refused as error:
            return refused(error)


class TOTPView(ManageView):
    def post(self, request):
        data = self.checked(ActivateTOTPSerializer)
        try:
            codes = services.activate_totp(request.user, data["code"])
        except Refused as error:
            return refused(error, "code")
        return Response({"recovery_codes": codes}, status=201)

    def delete(self, request):
        self.checked(PasswordSerializer)
        totp = services.authenticators(request.user).filter(type=services.Type.TOTP).first()
        if totp is None:
            raise Http404
        services.remove(totp)
        return Response(status=204)


class KeyOptionsView(ManageView):
    def post(self, request):
        with context.request_context(request):
            return Response(services.key_options(request.user))


class KeysView(ManageView):
    def post(self, request):
        data = self.checked(AddKeySerializer)
        try:
            with context.request_context(request):
                added, codes = services.add_key(request.user, data["name"], data["credential"])
        except Refused as error:
            return refused(error, "credential")
        return Response({"id": added.pk, "name": data["name"], "recovery_codes": codes}, status=201)


class KeyView(ManageView):
    def get_key(self, pk):
        found = services.key(self.request.user, pk)
        if found is None:
            raise Http404
        return found

    def patch(self, request, pk):
        found = self.get_key(pk)
        data = self.checked(NameSerializer)
        services.rename_key(found, data["name"])
        return Response({"id": found.pk, "name": data["name"]})

    def delete(self, request, pk):
        found = self.get_key(pk)
        self.checked(PasswordSerializer)
        services.remove(found)
        return Response(status=204)


class RecoveryCodesView(ManageView):
    def post(self, request):
        self.checked(PasswordSerializer)
        try:
            return Response({"recovery_codes": services.new_recovery_codes(request.user)})
        except Refused as error:
            return refused(error)


class SecondStepView(APIView):
    """Public: the mfa_token from LoginView stands in for the password, so a stale bearer
    token is ignored rather than refused."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = with_defaults(AuthThrottle)


class LoginKeyOptionsView(SecondStepView):
    def post(self, request):
        try:
            step = services.pending(request.data.get("mfa_token"))
            with context.request_context(request):
                return Response(services.login_key_options(step))
        except Refused as error:
            return refused(error)


class LoginView(SecondStepView):
    def post(self, request):
        try:
            step = services.pending(request.data.get("mfa_token"))
            if request.data.get("credential"):
                with context.request_context(request):
                    tokens = services.finish_with_key(step, request.data["credential"])
            else:
                tokens = services.finish_with_code(step, str(request.data.get("code", "")))
        except Refused as error:
            return refused(error)
        return Response(tokens)
