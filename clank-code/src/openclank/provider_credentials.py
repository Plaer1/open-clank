"""Versioned, owner-bound provider credential envelopes.

The existing application key remains the only installation secret.  A
domain-separated AES-256-GCM key is derived from it; authenticated data binds
the ciphertext to its owner, connection, account, and credential schema
version so copying a DB value into another tenant or account fails closed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import secrets
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.secret_storage import _load_or_create_key, keyed_digest


_ENVELOPE_VERSION = 1
_ALGORITHM = "AES-256-GCM"
_KEY_CONTEXT = b"open-clank/provider-credential-envelope/v1"


class CredentialEnvelopeError(ValueError):
    """A credential envelope is malformed, corrupt, or out of scope."""


@dataclass(frozen=True)
class CredentialScope:
    owner: str
    connection_id: str
    account_id: str
    credential_version: int = _ENVELOPE_VERSION

    def __post_init__(self) -> None:
        for field_name in ("owner", "connection_id", "account_id"):
            value = str(getattr(self, field_name) or "").strip()
            if not value:
                raise CredentialEnvelopeError(f"{field_name} is required")
            if "\x00" in value:
                raise CredentialEnvelopeError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, value)
        if int(self.credential_version) < 1:
            raise CredentialEnvelopeError("credential_version must be positive")
        object.__setattr__(self, "credential_version", int(self.credential_version))


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    if not isinstance(payload, Mapping) or not payload:
        raise CredentialEnvelopeError("credential payload must be a non-empty object")
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CredentialEnvelopeError("credential payload is not JSON-compatible") from exc
    return encoded


def _derived_key() -> bytes:
    master = _load_or_create_key()
    return hmac.new(master, _KEY_CONTEXT, hashlib.sha256).digest()


def _aad(scope: CredentialScope) -> bytes:
    return json.dumps(
        [
            "open-clank-provider-credential",
            scope.owner,
            scope.connection_id,
            scope.account_id,
            scope.credential_version,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def seal_credential(payload: Mapping[str, Any], scope: CredentialScope) -> str:
    """Return a self-describing AEAD envelope without exposing plaintext."""

    plaintext = _canonical_payload(payload)
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_derived_key()).encrypt(nonce, plaintext, _aad(scope))
    return json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "alg": _ALGORITHM,
            "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def unseal_credential(envelope: str, scope: CredentialScope) -> dict[str, Any]:
    """Decrypt one envelope only when its exact authenticated scope matches."""

    try:
        parsed = json.loads(str(envelope or ""))
        if not isinstance(parsed, dict):
            raise TypeError
        if parsed.get("v") != _ENVELOPE_VERSION or parsed.get("alg") != _ALGORITHM:
            raise CredentialEnvelopeError("unsupported credential envelope")
        nonce = base64.b64decode(parsed["nonce"], altchars=b"-_", validate=True)
        ciphertext = base64.b64decode(
            parsed["ciphertext"],
            altchars=b"-_",
            validate=True,
        )
        if len(nonce) != 12:
            raise ValueError
        plaintext = AESGCM(_derived_key()).decrypt(
            nonce,
            ciphertext,
            _aad(scope),
        )
        payload = json.loads(plaintext.decode("utf-8"))
        if not isinstance(payload, dict) or not payload:
            raise ValueError
        return payload
    except CredentialEnvelopeError:
        raise
    except Exception as exc:
        # Deliberately omit both the ciphertext and provider error details.
        raise CredentialEnvelopeError("credential envelope could not be opened") from exc


def credential_fingerprint(
    payload: Mapping[str, Any],
    *,
    owner: str,
    connection_id: str,
) -> str:
    """Keyed duplicate detector scoped to one owner's exact connection."""

    canonical = _canonical_payload(payload).decode("utf-8")
    return keyed_digest(
        canonical,
        context=f"provider-credential:{owner}:{connection_id}",
    )


__all__ = [
    "CredentialEnvelopeError",
    "CredentialScope",
    "credential_fingerprint",
    "seal_credential",
    "unseal_credential",
]
