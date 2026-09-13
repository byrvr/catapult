"""Persisted Apple development signing identity.

Free-account development certificates are valid for a year; only the
provisioning profile carries the 7-day clock. Catapult used to revoke every
certificate and mint a new keypair on each call, including on every hourly
refresh, which threw away a year-long credential and revoked whatever
certificate Xcode, AltStore, or a second Mac was using on the same Apple ID.

The identity is cached in the login Keychain (the private key is secret; the
certificate is not, but keeping them together keeps the pair consistent).
When the Keychain cannot be written or read — a locked keychain, a launch
agent without keychain access, a broken ``security`` binary — it falls back to
a 0600 file under ``~/.catapult``: an identity that silently fails to persist
means a new certificate on every signing call, which is exactly the churn this
module exists to stop.

Reuse is decided against Apple's certificate list. Apple's metadata is
unreliable — ``serialNumber`` is often empty in the CSR response and
``certificateId`` is missing from some listed entries — so a stored
certificate is matched on the ids when they line up, and otherwise on the
certificate itself: its x509 serial and the DER Apple returns as
``certContent``. Without that, an identity Apple still lists looked "revoked
elsewhere" and a fresh certificate was minted per call.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

KEYCHAIN_ACCOUNT_PREFIX = "signing-cert"

# Renew rather than start a refresh with a credential about to lapse mid-flight.
RENEW_BEFORE = _dt.timedelta(days=7)

# Where the file fallback lives. None means ~/.catapult; tests point it at a
# temporary directory.
FALLBACK_DIR: Path | None = None


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


def _not_after(cert: x509.Certificate) -> _dt.datetime:
    try:
        return cert.not_valid_after_utc.replace(tzinfo=None)
    except AttributeError:  # cryptography < 42
        return cert.not_valid_after


def certificate_not_after(cert_pem: bytes) -> _dt.datetime:
    """Return the certificate's notAfter as a naive UTC datetime."""
    return _not_after(x509.load_pem_x509_certificate(cert_pem))


def _normalize_serial(value: object) -> str:
    """Apple prints serials as upper-case hex without leading zeros; match that
    shape whether the other side came from Apple or from ``format(n, "X")``."""
    return str(value or "").strip().upper().replace(":", "").lstrip("0")


def _same_serial(a: object, b: object) -> bool:
    left, right = _normalize_serial(a), _normalize_serial(b)
    return bool(left) and left == right


def load_listed_certificate(content: object) -> x509.Certificate | None:
    """Parse a ``certContent`` value from ``listAllDevelopmentCerts``.

    Apple returns DER in a plist ``<data>``; be lenient and also accept PEM or
    base64 text, since the same helper serves cached responses and tests.
    """
    if isinstance(content, str):
        content = content.encode("ascii", "ignore")
    if not isinstance(content, (bytes, bytearray)) or not content:
        return None
    raw = bytes(content)
    for loader in (x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            return loader(raw)
        except Exception:
            continue
    try:
        return x509.load_der_x509_certificate(base64.b64decode(raw))
    except Exception:
        return None


def _listed_matches(identity: SigningIdentity, ours: x509.Certificate, cert: dict) -> bool:
    listed_id = str(cert.get("certificateId") or "")
    listed_serial = cert.get("serialNumber") or ""

    if identity.certificate_id and listed_id == identity.certificate_id:
        return True
    if identity.serial_number and _same_serial(listed_serial, identity.serial_number):
        return True
    # The CSR response often carries no serial at all; the certificate does.
    if _same_serial(listed_serial, format(ours.serial_number, "X")):
        return True

    listed = load_listed_certificate(cert.get("certContent"))
    if listed is None:
        return False
    try:
        return (
            listed.public_bytes(serialization.Encoding.DER)
            == ours.public_bytes(serialization.Encoding.DER)
        )
    except Exception:
        return False


def is_usable(
    identity: SigningIdentity,
    apple_certs: list[dict],
    now: _dt.datetime | None = None,
) -> bool:
    """True when Apple still lists this certificate and it is not near expiry.

    Apple omits serialNumber for some recently-created certificates and
    certificateId for others, so match on either id — and, failing both, on
    the certificate itself (x509 serial, then the listed DER content).
    """
    now = now or _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)

    try:
        ours = x509.load_pem_x509_certificate(identity.cert_pem)
    except Exception:
        logger.debug("Stored signing certificate could not be parsed", exc_info=True)
        return False
    not_after = _not_after(ours)
    if not_after - RENEW_BEFORE <= now:
        logger.info("Stored signing certificate expires %s — renewing", not_after)
        return False

    for cert in apple_certs:
        if _listed_matches(identity, ours, cert):
            return True

    logger.info("Apple no longer lists our signing certificate — it was revoked elsewhere")
    return False


def _account(team_id: str) -> str:
    return f"{KEYCHAIN_ACCOUNT_PREFIX}:{team_id}"


# ── File fallback ──


def _fallback_path(team_id: str) -> Path:
    base = FALLBACK_DIR or (Path.home() / ".catapult")
    safe = "".join(ch for ch in team_id if ch.isalnum() or ch in "-_") or "team"
    return base / f"signing-identity-{safe}.json"


def _read_fallback(team_id: str) -> str | None:
    path = _fallback_path(team_id)
    try:
        return path.read_text("utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("Could not read the signing identity fallback %s", path, exc_info=True)
        return None


def _write_fallback(team_id: str, payload: str) -> bool:
    path = _fallback_path(team_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(path, 0o600)
        return True
    except OSError:
        logger.warning("Could not write the signing identity fallback %s", path, exc_info=True)
        return False


def _remove_fallback(team_id: str) -> None:
    try:
        _fallback_path(team_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not remove the signing identity fallback", exc_info=True)


# ── Persistence ──


def load(team_id: str) -> SigningIdentity | None:
    from catapult.refresh import _keychain_get

    raw = None
    try:
        raw = _keychain_get(_account(team_id))
    except Exception:
        logger.warning("Keychain lookup for the signing identity failed", exc_info=True)
    source = "Keychain"
    if not raw:
        raw = _read_fallback(team_id)
        source = "file"
    if not raw:
        return None
    try:
        return SigningIdentity.from_json(raw)
    except Exception:
        logger.warning("Stored signing identity (%s) is unreadable — discarding", source)
        return None


def save(team_id: str, identity: SigningIdentity) -> None:
    from catapult.refresh import _keychain_set

    payload = identity.to_json()
    stored = False
    try:
        stored = _keychain_set(_account(team_id), payload)
    except Exception:
        logger.warning("Keychain write for the signing identity failed", exc_info=True)
    if stored:
        # The Keychain is authoritative again; do not leave a stale copy behind.
        _remove_fallback(team_id)
        return
    path = _fallback_path(team_id)
    if _write_fallback(team_id, payload):
        logger.warning(
            "Could not persist signing identity in Keychain — keeping it in %s instead", path
        )
    else:
        logger.error(
            "Could not persist signing identity anywhere; the next signing call will "
            "mint another certificate"
        )


def clear(team_id: str) -> None:
    from catapult.refresh import _keychain_delete

    try:
        _keychain_delete(_account(team_id))
    except Exception:
        logger.debug("Keychain delete for the signing identity failed", exc_info=True)
    _remove_fallback(team_id)
