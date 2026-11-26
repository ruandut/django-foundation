from django.contrib import admin

from .captcha import AdminLoginForm


def protect_admin_login(site: admin.AdminSite = admin.site) -> None:
    """Give the Django admin's login the same captcha as the app's. Call it from urls.py."""
    site.login_form = AdminLoginForm
    site.login_template = "foundation_security/admin_login.html"
