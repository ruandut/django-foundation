from allauth.core import context
from allauth.mfa.adapter import DefaultMFAAdapter
from django.conf import settings

from . import crypto


class MFAAdapter(DefaultMFAAdapter):
    """allauth.mfa's hooks: secrets encrypted at rest, and names that don't need its sessions or
    the sites framework."""

    def encrypt(self, text: str) -> str:
        return crypto.encrypt(text)

    def decrypt(self, encrypted_text: str) -> str:
        return crypto.decrypt(encrypted_text)

    def get_totp_issuer(self) -> str:
        return settings.MFA_TOTP_ISSUER

    def get_public_key_credential_rp_entity(self) -> dict[str, str]:
        rp_id = settings.MFA_WEBAUTHN_RP_ID or context.request.get_host().partition(":")[0]
        return {"id": rp_id, "name": settings.MFA_TOTP_ISSUER}
