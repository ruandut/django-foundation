"""Include next to the login route, e.g. path("api/auth/mfa/", include("foundation.mfa.urls"))."""

from django.urls import path

from . import views

urlpatterns = [
    path("", views.OverviewView.as_view(), name="mfa"),
    path("totp/setup/", views.TOTPSetupView.as_view(), name="mfa-totp-setup"),
    path("totp/", views.TOTPView.as_view(), name="mfa-totp"),
    path("keys/options/", views.KeyOptionsView.as_view(), name="mfa-key-options"),
    path("keys/", views.KeysView.as_view(), name="mfa-keys"),
    path("keys/<int:pk>/", views.KeyView.as_view(), name="mfa-key"),
    path("recovery-codes/", views.RecoveryCodesView.as_view(), name="mfa-recovery-codes"),
    path("login/key-options/", views.LoginKeyOptionsView.as_view(), name="mfa-login-key-options"),
    path("login/", views.LoginView.as_view(), name="mfa-login"),
]
