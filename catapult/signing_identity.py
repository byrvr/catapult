"""Persisted Apple development signing identity.

Free-account development certificates are valid for a year; only the
provisioning profile carries the 7-day clock. Catapult used to revoke every
certificate and mint a new keypair on each call, including on every hourly
refresh, which threw away a year-long credential and revoked whatever
certificate Xcode, AltStore, or a second Mac was using on the same Apple ID.

The identity is cached in the login Keychain (the private key is secret; the
certificate is not, but keeping them together keeps the pair consistent).
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

KEYCHAIN_ACCOUNT_PREFIX = "signing-cert"

# Renew rather than start a refresh with a credential about to lapse mid-flight.
RENEW_BEFORE = _dt.timedelta(days=7)


@dataclass(frozen=True)
class SigningIdentity:
    cert_pem: bytes
    key_pem: bytes
    certificate_id: str
    serial_number: str

    def private_key(self) -> rsa.RSAPrivateKey:
        return serialization.load_pem_private_key(self.key_pem, password=None)

    def to_json(self) -> str:
        return json.dumps({
            "cert_pem": base64.b64encode(self.cert_pem).decode("ascii"),
            "key_pem": base64.b64encode(self.key_pem).decode("ascii"),
            "certificate_id": self.certificate_id,
            "serial_number": self.serial_number,
        })

    @classmethod
    def from_json(cls, raw: str) -> "SigningIdentity":
        data = json.loads(raw)
        return cls(
            cert_pem=base64.b64decode(data["cert_pem"]),
            key_pem=base64.b64decode(data["key_pem"]),
            certificate_id=data.get("certificate_id", ""),
            serial_number=data.get("serial_number", ""),
        )

    @classmethod
    def from_key(
        cls,
        cert_pem: bytes,
        private_key: rsa.RSAPrivateKey,
        certificate_id: str,
        serial_number: str,
    ) -> "SigningIdentity":
        return cls(
            cert_pem=cert_pem,
            key_pem=private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            certificate_id=certificate_id,
            serial_number=serial_number,
        )


def certificate_not_after(cert_pem: bytes) -> _dt.datetime:
    """Return the certificate's notAfter as a naive UTC datetime."""
    cert = x509.load_pem_x509_certificate(cert_pem)
    try:
        return cert.not_valid_after_utc.replace(tzinfo=None)
    except AttributeError:  # cryptography < 42
        return cert.not_valid_after


def is_usable(
    identity: SigningIdentity,
    apple_certs: list[dict],
    now: _dt.datetime | None = None,
) -> bool:
    """True when Apple still lists this certificate and it is not near expiry.

    Apple omits serialNumber for some recently-created certificates and
    certificateId for others, so match on either.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    try:
        not_after = certificate_not_after(identity.cert_pem)
    except Exception:
        logger.debug("Stored signing certificate could not be parsed", exc_info=True)
        return False
    if not_after - RENEW_BEFORE <= now:
        logger.info("Stored signing certificate expires %s — renewing", not_after)
        return False

    for cert in apple_certs:
        listed_id = cert.get("certificateId", "")
        listed_serial = cert.get("serialNumber", "")
        if identity.certificate_id and listed_id == identity.certificate_id:
            return True
        if identity.serial_number and listed_serial == identity.serial_number:
            return True

    logger.info("Apple no longer lists our signing certificate — it was revoked elsewhere")
    return False


def _account(team_id: str) -> str:
    return f"{KEYCHAIN_ACCOUNT_PREFIX}:{team_id}"


def load(team_id: str) -> SigningIdentity | None:
    from catapult.refresh import _keychain_get

    raw = _keychain_get(_account(team_id))
    if not raw:
        return None
    try:
        return SigningIdentity.from_json(raw)
    except Exception:
        logger.warning("Stored signing identity is unreadable — discarding")
        return None


def save(team_id: str, identity: SigningIdentity) -> None:
    from catapult.refresh import _keychain_set

    if not _keychain_set(_account(team_id), identity.to_json()):
        logger.warning("Could not persist signing identity in Keychain")


def clear(team_id: str) -> None:
    from catapult.refresh import _keychain_delete

    _keychain_delete(_account(team_id))
