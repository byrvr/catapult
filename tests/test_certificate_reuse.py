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


MACHINE = "MACHINE-UUID-1"


class Harness:
    """A DeveloperServices whose Apple calls are scripted.

    ``revocations`` records the scope of every revocation Catapult asked for;
    ``revoke_counts`` scripts how many certificates each of those found.
    """

    def __init__(self, monkeypatch, *, stored=None, csr_result_codes=(), revoke_counts=()):
        self.services = DeveloperServices()
        self.requests: list[str] = []
        self.csr_fields: list[dict] = []
        self.revocations: list[dict] = []
        self.saved: list[object] = []
        codes = list(csr_result_codes)
        counts = list(revoke_counts)
        self.cert_pem, _ = _cert_pem()

        async def fake_request(session, action, fields=None):
            self.requests.append(action)
            if action == CSR:
                self.csr_fields.append(dict(fields or {}))
                code = codes.pop(0) if codes else 0
                if code:
                    raise DeveloperServicesError("blocked", result_code=code)
                return {"certRequest": {"certificateId": "NEWID", "serialNumber": "NEWSERIAL"}}
            if action == "ios/listAllDevelopmentCerts.action":
                return {"certificates": [{"certificateId": "ABC123", "serialNumber": "1234"}]}
            raise AssertionError(f"unexpected request {action}")

        async def fake_revoke(session, team_id, *, catapult_only=False, machine_id=None):
            scope = {"catapult_only": catapult_only}
            if machine_id is not None:
                scope["machine_id"] = machine_id
            self.revocations.append(scope)
            return counts.pop(0) if counts else 1

        monkeypatch.setattr("catapult.developer.RETRY_DELAY_SECONDS", 0)
        monkeypatch.setattr(DeveloperServices, "_machine_id", staticmethod(lambda: MACHINE))

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

    assert h.revocations == [{"catapult_only": False}]  # personal team: every cert may go
    assert h.requests.count(CSR) == 2


async def test_paid_team_revokes_this_macs_certificates_first(monkeypatch):
    """A blocked CSR on a paid team is almost always this Mac's own stale or
    pending request. Revoking every Catapult certificate on the team — the old
    behaviour — sent the account holder one revocation mail per certificate."""
    h = Harness(monkeypatch, csr_result_codes=[7460, 0])

    await h.services.get_or_create_cert(object(), TEAM, personal_team=False)

    assert h.revocations == [{"catapult_only": True, "machine_id": MACHINE}]
    assert h.requests.count(CSR) == 2


async def test_paid_team_widens_to_all_catapult_certs_only_if_still_blocked(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[7460, 7460, 0])

    await h.services.get_or_create_cert(object(), TEAM, personal_team=False)

    assert h.revocations == [
        {"catapult_only": True, "machine_id": MACHINE},
        {"catapult_only": True},
    ]
    assert h.requests.count(CSR) == 3


async def test_paid_team_skips_the_retry_when_nothing_of_this_mac_was_found(monkeypatch):
    """No certificate carried this Mac's id, so the same CSR would be refused
    again: widen straight away rather than ask Apple twice."""
    h = Harness(monkeypatch, csr_result_codes=[7460, 0], revoke_counts=[0, 3])

    await h.services.get_or_create_cert(object(), TEAM, personal_team=False)

    assert h.revocations == [
        {"catapult_only": True, "machine_id": MACHINE},
        {"catapult_only": True},
    ]
    assert h.requests.count(CSR) == 2


async def test_paid_team_gives_up_after_the_widest_scope(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[7460, 7460, 7460])

    with pytest.raises(DeveloperServicesError) as excinfo:
        await h.services.get_or_create_cert(object(), TEAM, personal_team=False)

    assert excinfo.value.result_code == 7460
    assert h.requests.count(CSR) == 3
    assert h.revocations[-1] == {"catapult_only": True}


async def test_csr_carries_this_macs_stable_machine_id(monkeypatch):
    """A random UUID per request made every certificate look like a different
    machine's, so nothing could tell this Mac's stale certificates apart."""
    h = Harness(monkeypatch)

    await h.services.get_or_create_cert(object(), TEAM)

    assert [f["machineId"] for f in h.csr_fields] == [MACHINE]
    assert h.csr_fields[0]["machineName"] == "Catapult"


async def test_other_csr_errors_propagate_without_revoking(monkeypatch):
    h = Harness(monkeypatch, csr_result_codes=[9999])

    with pytest.raises(DeveloperServicesError):
        await h.services.get_or_create_cert(object(), TEAM)

    assert h.revocations == []


async def test_revocation_scope_and_count(monkeypatch):
    """Only Catapult's certificates from this Mac are revoked, and the count
    reflects what was actually sent to Apple — a certificate with neither a
    serial nor an id cannot be revoked and must not make a retry look useful."""
    services = DeveloperServices()
    requests: list[tuple[str, dict]] = []

    async def fake_request(session, action, fields=None):
        requests.append((action, dict(fields or {})))
        if action == "ios/listAllDevelopmentCerts.action":
            return {"certificates": [
                {"machineName": "Catapult", "machineId": MACHINE, "serialNumber": "1"},
                {"machineName": "Catapult", "machineId": MACHINE, "certificateId": "ID2"},
                {"machineName": "Catapult", "machineId": MACHINE},
                {"machineName": "Catapult", "machineId": "OTHER-MAC", "serialNumber": "9"},
                {"machineName": "Xcode", "serialNumber": "7"},
            ]}
        if action == "ios/revokeDevelopmentCert.action":
            return {}
        raise AssertionError(f"unexpected request {action}")

    services._request = fake_request

    revoked = await services._revoke_all_certs(
        object(), TEAM, catapult_only=True, machine_id=MACHINE
    )

    assert revoked == 2
    assert [f for a, f in requests if a == "ios/revokeDevelopmentCert.action"] == [
        {"teamId": TEAM, "serialNumber": "1"},
        {"teamId": TEAM, "certificateId": "ID2"},
    ]
