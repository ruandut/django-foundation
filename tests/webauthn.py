"""A software security key, enough for fido2's server checks: it makes an ES256 credential with
"none" attestation and signs assertions, as a browser's navigator.credentials would hand them
over (the JSON form, base64url everywhere)."""

import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fido2.cose import ES256
from fido2.utils import sha256, websafe_decode, websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
)

ORIGIN = "https://testserver"


class SoftKey:
    def __init__(self, rp_id: str = "testserver", origin: str = ORIGIN):
        self.rp_id = rp_id
        self.origin = origin
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.counter = 0

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}
        ).encode()

    def create(self, options: dict) -> dict:
        """The answer to navigator.credentials.create({publicKey: options["publicKey"]})."""
        public_key = options["publicKey"]
        credential = AttestedCredentialData.create(
            Aaguid.NONE,
            self.credential_id,
            ES256.from_cryptography_key(self.private_key.public_key()),
        )
        auth_data = AuthenticatorData.create(
            sha256(self.rp_id.encode()),
            AuthenticatorData.FLAG.UP | AuthenticatorData.FLAG.AT,
            self.counter,
            bytes(credential),
        )
        attestation = AttestationObject.create("none", auth_data, {})
        raw_id = websafe_encode(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(
                    self._client_data("webauthn.create", public_key["challenge"])
                ),
                "attestationObject": websafe_encode(bytes(attestation)),
            },
            "clientExtensionResults": {"credProps": {"rk": False}},
        }

    def get(self, options: dict) -> dict:
        """The answer to navigator.credentials.get({publicKey: options["publicKey"]})."""
        self.counter += 1
        client_data = self._client_data("webauthn.get", options["publicKey"]["challenge"])
        auth_data = bytes(
            AuthenticatorData.create(
                sha256(self.rp_id.encode()), AuthenticatorData.FLAG.UP, self.counter
            )
        )
        signature = self.private_key.sign(
            auth_data + sha256(client_data), ec.ECDSA(hashes.SHA256())
        )
        raw_id = websafe_encode(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": websafe_encode(client_data),
                "authenticatorData": websafe_encode(auth_data),
                "signature": websafe_encode(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


__all__ = ["SoftKey", "websafe_decode"]
