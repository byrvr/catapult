"""get_or_create_cert(): reuse first, ask Apple second, revoke last.

Free-account certificates last a year; only the profile carries the 7-day
clock. Revoking before every CSR meant two Catapult Macs on one Apple ID, which
is the sync feature's whole point, took turns killing each other's certificate
whenever one of them had no stored identity. Apple already answers a blocked
CSR with result code 7460, so revoke only when it says so.
"""

import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from catapult import signing_identity
from catapult.developer import DeveloperServices, DeveloperServicesError

TEAM = "TEAM123"
CSR = "ios/submitDevelopmentCSR.action"


def _cert_pem(not_after=dt.datetime(2027, 1, 1)):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple Development: Test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(0x1234)
        .not_valid_before(dt.datetime(2026, 1, 1))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM), key


class Harness:
    """A DeveloperServices whose Apple calls are scripted."""

    def __init__(self, monkeypatch, *, stored=None, csr_result_codes=()):
        self.services = DeveloperServices()
        self.requests: list[str] = []
        self.revocations: list[bool] = []
        self.saved: list[object] = []
        codes = list(csr_result_codes)
        self.cert_pem, _ = _cert_pem()

        async def fake_request(session, action, fields=None):
            self.requests.append(action)
            if action == CSR:
                code = codes.pop(0) if codes else 0
                if code:
                    raise DeveloperServicesError("blocked", result_code=code)
                return {"certRequest": {"certificateId": "NEWID", "serialNumber": "NEWSERIAL"}}
            if action == "ios/listAllDevelopmentCerts.action":
                return {"certificates": [{"certificateId": "ABC123", "serialNumber": "1234"}]}
            raise AssertionError(f"unexpected request {action}")

        async def fake_revoke(session, team_id, *, catapult_only=False):
            self.revocations.append(catapult_only)

        async def fake_download(session, team_id):
            return self.cert_pem

        self.services._request = fake_request
        self.services._revoke_all_certs = fake_revoke
        self.services._download_cert_content = fake_download
        monkeypatch.setattr(signing_identity, "load", lambda team_id: stored)
        monkeypatch.setattr(signing_identity, "save", lambda team_id, identity: self.saved.append(identity))
        monkeypatch.setattr(signing_identity, "clear", lambda team_id: None)


async def test_reuses_a_stored_certificate_apple_still_lists(monkeypatch):
    cert_pem, key = _cert_pem()
    stored = signing_identity.SigningIdentity.from_key(cert_pem, key, "ABC123", "1234")
    h = Harness(monkeypatch, stored=stored)

    pem, _ = await h.services.get_or_create_cert(object(), TEAM)

    assert pem == cert_pem
    assert CSR not in h.requests
    assert h.revocations == []


async def test_asks_apple_before_revoking_anything(monkeypatch):
    """No stored identity: submit the CSR first. Apple accepts, nothing is revoked."""
    h = Harness(monkeypatch)

    await h.services.get_or_create_cert(object(), TEAM)

    assert h.revocations == []
    assert h.requests.count(CSR) == 1
    assert h.saved, "the new identity must be persisted for reuse"


async def test_revokes_only_when_apple_says_the_slot_is_taken(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[7460, 0])

    await h.services.get_or_create_cert(object(), TEAM)

    assert h.revocations == [False]  # personal team: every cert may go
    assert h.requests.count(CSR) == 2


async def test_paid_team_revocation_is_scoped_to_catapult_certs(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[7460, 0])

    await h.services.get_or_create_cert(object(), TEAM, personal_team=False)

    assert h.revocations == [True]


async def test_other_csr_errors_propagate_without_revoking(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[9999])

    with pytest.raises(DeveloperServicesError):
        await h.services.get_or_create_cert(object(), TEAM)

    assert h.revocations == []
