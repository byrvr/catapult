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


# ── Matching on the certificate itself ──


def _listed(cert_pem: bytes, **fields) -> dict:
    """An entry the way listAllDevelopmentCerts returns it: DER in certContent."""
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    return {"certContent": der, **fields}


def _own_identity(cert_pem: bytes, key: rsa.RSAPrivateKey, *, certificate_id="", serial_number=""):
    return signing_identity.SigningIdentity.from_key(cert_pem, key, certificate_id, serial_number)


def test_matches_on_x509_serial_when_the_csr_response_had_none():
    """Apple's CSR response frequently carries an empty serialNumber and its
    listing omits certificateId for some entries, so an identity Apple still
    listed looked revoked and a fresh certificate was minted per call."""
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1), serial=0x0A1B2C)
    identity = _own_identity(cert_pem, key, certificate_id="NEWID", serial_number="")
    apple_certs = [{"serialNumber": "A1B2C"}]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_serial_comparison_ignores_case_and_leading_zeros():
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1), serial=0x0A1B2C)
    identity = _own_identity(cert_pem, key, serial_number="0a1b2c")
    apple_certs = [{"serialNumber": "000A1B2C"}]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_matches_on_listed_certificate_content_without_any_ids():
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = _own_identity(cert_pem, key)
    apple_certs = [_listed(cert_pem)]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_matches_on_base64_certificate_content():
    import base64

    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = _own_identity(cert_pem, key)
    der = _listed(cert_pem)["certContent"]
    apple_certs = [{"certContent": base64.b64encode(der).decode("ascii")}]

    assert signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


def test_a_different_certificate_with_garbage_ids_does_not_match():
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1), serial=0x1111)
    other_pem, _ = _make_cert(dt.datetime(2027, 1, 1), serial=0x2222)
    identity = _own_identity(cert_pem, key)
    apple_certs = [
        _listed(other_pem, certificateId="OTHER", serialNumber="2222"),
        {"certContent": b"not a certificate", "serialNumber": ""},
        {"certContent": None},
    ]

    assert not signing_identity.is_usable(identity, apple_certs, now=dt.datetime(2026, 8, 2))


# ── Persistence: Keychain first, file fallback second ──


def _keychain_stub(monkeypatch, *, store: dict | None, writable: bool):
    """Route the Keychain helpers to a dict, or make them fail outright."""
    from catapult import refresh

    def get(account):
        if store is None:
            raise OSError("security: keychain locked")
        return store.get(account)

    def set_(account, data):
        if store is None or not writable:
            return False
        store[account] = data
        return True

    def delete(account):
        if store is not None:
            store.pop(account, None)

    monkeypatch.setattr(refresh, "_keychain_get", get)
    monkeypatch.setattr(refresh, "_keychain_set", set_)
    monkeypatch.setattr(refresh, "_keychain_delete", delete)


def test_falls_back_to_a_private_file_when_the_keychain_rejects_the_write(tmp_path, monkeypatch):
    """An identity that fails to persist means a new certificate on every
    signing call, which is the churn this module exists to stop."""
    import os
    import stat

    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    _keychain_stub(monkeypatch, store={}, writable=False)
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = _own_identity(cert_pem, key, certificate_id="ID1", serial_number="1")

    signing_identity.save("TEAM1", identity)

    path = tmp_path / "signing-identity-TEAM1.json"
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    restored = signing_identity.load("TEAM1")
    assert restored is not None
    assert restored.certificate_id == "ID1"
    assert restored.cert_pem == cert_pem


def test_loads_from_the_file_when_the_keychain_itself_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    _keychain_stub(monkeypatch, store=None, writable=False)
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = _own_identity(cert_pem, key, certificate_id="ID2", serial_number="2")

    signing_identity.save("TEAM2", identity)

    restored = signing_identity.load("TEAM2")
    assert restored is not None and restored.certificate_id == "ID2"


def test_keychain_stays_authoritative_and_clears_the_file_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    store: dict = {}
    _keychain_stub(monkeypatch, store=store, writable=True)
    cert_pem, key = _make_cert(dt.datetime(2027, 1, 1))
    identity = _own_identity(cert_pem, key, certificate_id="ID3", serial_number="3")
    (tmp_path / "signing-identity-TEAM3.json").write_text("stale")

    signing_identity.save("TEAM3", identity)

    assert not (tmp_path / "signing-identity-TEAM3.json").exists()
    assert signing_identity.load("TEAM3").certificate_id == "ID3"
    assert list(store) == ["signing-cert:TEAM3"]


def test_clear_removes_both_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    store: dict = {"signing-cert:TEAM4": "x"}
    _keychain_stub(monkeypatch, store=store, writable=True)
    (tmp_path / "signing-identity-TEAM4.json").write_text("x")

    signing_identity.clear("TEAM4")

    assert store == {}
    assert not (tmp_path / "signing-identity-TEAM4.json").exists()
    assert signing_identity.load("TEAM4") is None
