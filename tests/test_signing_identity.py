"""Persisted signing identity.

get_or_create_cert() used to revoke every development certificate and mint a
fresh keypair on every call, including on every hourly refresh. Free-account
certificates are valid for a YEAR (only the provisioning profile carries the
7-day clock), so this destroyed a long-lived credential hourly and, worse,
revoked the certificate belonging to any other machine or tool on the same
Apple ID — Xcode, AltStore, a second Mac.
"""

import datetime as dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from catapult import signing_identity


def _make_cert(not_after: dt.datetime, serial: int = 0x1234) -> tuple[bytes, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple Development: Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(dt.datetime(2026, 1, 1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), key


def test_round_trips_through_serialization():
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = signing_identity.SigningIdentity(
        cert_pem=cert_pem,
        key_pem=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        certificate_id="ABC123",
        serial_number="1234",
    )

    restored = signing_identity.SigningIdentity.from_json(identity.to_json())

    assert restored.certificate_id == "ABC123"
    assert restored.serial_number == "1234"
    assert restored.cert_pem == cert_pem
    assert restored.private_key().public_key().public_numbers() == key.public_key().public_numbers()


def test_certificate_not_after_is_read_from_the_cert():
    not_after = dt.datetime(2027, 7, 17)
    cert_pem, _ = _make_cert(not_after)

    parsed = signing_identity.certificate_not_after(cert_pem)

    assert parsed.year == 2027 and parsed.month == 7 and parsed.day == 17


def test_usable_when_unexpired_and_still_listed_by_apple():
    cert_pem, _ = _make_cert(dt.datetime(2027, 1, 1))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")
    apple_certs = [{"certificateId": "ABC123", "serialNumber": "1234"}]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_not_usable_when_apple_no_longer_lists_it():
    """Another machine or Xcode revoked it — we must mint a new one."""
    cert_pem, _ = _make_cert(dt.datetime(2027, 1, 1))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")
    apple_certs = [{"certificateId": "SOMETHINGELSE", "serialNumber": "9999"}]

    assert not signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_not_usable_when_expired():
    cert_pem, _ = _make_cert(dt.datetime(2026, 1, 2))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")
    apple_certs = [{"certificateId": "ABC123", "serialNumber": "1234"}]

    assert not signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_not_usable_when_close_to_expiry():
    """Renew before the edge rather than mid-refresh."""
    cert_pem, _ = _make_cert(dt.datetime(2026, 8, 3))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")
    apple_certs = [{"certificateId": "ABC123", "serialNumber": "1234"}]

    assert not signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_matches_on_serial_when_apple_omits_certificate_id():
    """Apple's responses do not always carry certificateId for recent certs."""
    cert_pem, _ = _make_cert(dt.datetime(2027, 1, 1))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")
    apple_certs = [{"serialNumber": "1234"}]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_not_usable_against_an_empty_apple_list():
    cert_pem, _ = _make_cert(dt.datetime(2027, 1, 1))
    identity = _identity(cert_pem, certificate_id="ABC123", serial_number="1234")

    assert not signing_identity.is_usable(identity, [], now=dt.datetime(2026, 8, 2))


def _identity(cert_pem: bytes, *, certificate_id: str, serial_number: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return signing_identity.SigningIdentity(
        cert_pem=cert_pem,
        key_pem=key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        certificate_id=certificate_id,
        serial_number=serial_number,
    )
