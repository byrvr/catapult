"""SideStore anisette v3: provisioning handshake, header assembly, recovery.

AOSKit stopped answering on macOS 26+ (``Info request failed: -45070``), so
the v3 protocol is what keeps signing alive there. The handshake and the
identity derivation follow SideStore's reference client: device id is the
16-byte identifier as a UUID, local user id its SHA-256, and the OTPs the
server signs are only valid together with that identity.
"""

import base64
import hashlib
import json
import os
import plistlib
import stat
import uuid

import httpx
import pytest

from catapult import anisette

SERVER = "https://ani.example.test"
SEED = "1B4E28BA-2FA1-11D2-883F-B9A761BDE3FB"
SERVER_CLIENT_INFO = "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.dt.Xcode/3594.4.19)>"
AKD_CLIENT_INFO = "<MacBookPro13,2> <macOS;13.1;22C65> <com.apple.AuthKit/1 (com.apple.akd/1.0)>"
START_URL = "https://gsa.apple.com/grandslam/MidService/startMachine"
FINISH_URL = "https://gsa.apple.com/grandslam/MidService/endMachine"


class FakeResponse:
    def __init__(self, status=200, content=b"", json_data=None):
        self.status_code = status
        self.content = content
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "https://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._json


class FakeHttp:
    """Scripted httpx.Client: records every call, answers by URL."""

    def __init__(self, log, *, lookup_503_for=None, headers_reply=None):
        self.log = log
        self.lookup_503_for = lookup_503_for
        self.headers_reply = headers_reply if headers_reply is not None else [
            {"result": "Headers", "X-Apple-I-MD": "OTP", "X-Apple-I-MD-M": "MACHINE", "X-Apple-I-MD-RINFO": "42"},
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, **kw):
        self.log.append(("GET", url, headers or {}, None))
        if url.endswith("/v3/client_info"):
            return FakeResponse(json_data={"client_info": SERVER_CLIENT_INFO, "user_agent": "akd/1.0 CFNetwork/808.1.4"})
        if url == anisette._GSA_LOOKUP_URL:
            if self.lookup_503_for and headers.get("X-Mme-Client-Info") == self.lookup_503_for:
                return FakeResponse(status=503)
            return FakeResponse(content=plistlib.dumps({"urls": {
                "midStartProvisioning": START_URL, "midFinishProvisioning": FINISH_URL}}))
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, content=None, headers=None, json=None, **kw):
        self.log.append(("POST", url, headers or {}, content if content is not None else json))
        if url == START_URL:
            return FakeResponse(content=plistlib.dumps({"Response": {"spim": "SPIM"}}))
        if url == FINISH_URL:
            return FakeResponse(content=plistlib.dumps({"Response": {"ptm": "PTM", "tk": "TK"}}))
        if url.endswith("/v3/get_headers"):
            reply = self.headers_reply.pop(0) if len(self.headers_reply) > 1 else self.headers_reply[0]
            return FakeResponse(json_data=reply)
        raise AssertionError(f"unexpected POST {url}")


class FakeSocket:
    def __init__(self, sent, adi_pb=b"ADI"):
        self.sent = sent
        self.incoming = [
            {"result": "GiveIdentifier"},
            {"result": "GiveStartProvisioningData"},
            {"result": "GiveEndProvisioningData", "cpim": "CPIM"},
            {"result": "ProvisioningSuccess", "adi_pb": base64.b64encode(adi_pb).decode()},
        ]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def recv(self, timeout=None):
        return json.dumps(self.incoming.pop(0))

    def send(self, text):
        self.sent.append(json.loads(text))


@pytest.fixture
def client(tmp_path, monkeypatch):
    log, sent, sockets = [], [], []

    def fake_connect(url, **kw):
        sockets.append(url)
        return FakeSocket(sent)

    import websockets.sync.client
    monkeypatch.setattr(websockets.sync.client, "connect", fake_connect)
    c = anisette.AnisetteV3Client(SERVER, tmp_path / "anisette-v3.json", SEED)
    c.log, c.sent, c.sockets = log, sent, sockets
    monkeypatch.setattr(c, "_http", lambda: FakeHttp(log))
    return c


def test_identity_is_derived_the_way_sidestore_does(client):
    ident = uuid.UUID(SEED).bytes
    assert client.identifier == ident
    assert client.device_id == SEED  # unchanged X-Mme-Device-Id across the source switch
    assert client.local_user_id == hashlib.sha256(ident).hexdigest()


def test_provisioning_handshake_and_persistence(client, tmp_path):
    client.provision()

    assert client.adi_pb == b"ADI"
    assert client.sockets == ["wss://ani.example.test/v3/provisioning_session"]
    assert client.sent == [
        {"identifier": base64.b64encode(uuid.UUID(SEED).bytes).decode()},
        {"spim": "SPIM"},
        {"ptm": "PTM", "tk": "TK"},
    ]
    apple_calls = [(m, u, h, b) for m, u, h, b in client.log if "apple.com" in u]
    assert [u for _, u, _, _ in apple_calls] == [anisette._GSA_LOOKUP_URL, START_URL, FINISH_URL]
    for _, _, headers, _ in apple_calls:
        assert headers["X-Mme-Client-Info"] == SERVER_CLIENT_INFO  # verbatim for provisioning
        assert headers["User-Agent"] == "akd/1.0 CFNetwork/808.1.4"
        assert headers["X-Mme-Device-Id"] == SEED
        assert headers["X-Apple-I-MD-LU"] == client.local_user_id
    assert plistlib.loads(apple_calls[2][3]) == {"Header": {}, "Request": {"cpim": "CPIM"}}

    path = tmp_path / "anisette-v3.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert base64.b64decode(saved["adi_pb"]) == b"ADI"
    assert saved["client_info"] == SERVER_CLIENT_INFO

    # A fresh client picks the identity back up without provisioning again.
    again = anisette.AnisetteV3Client(SERVER, path, "ignored")
    assert again.identifier == client.identifier and again.adi_pb == b"ADI"


def test_headers_provision_on_first_use_and_bind_the_identity(client):
    headers = client.headers()

    assert headers["X-Apple-I-MD"] == "OTP"
    assert headers["X-Apple-I-MD-M"] == "MACHINE"
    assert headers["X-Apple-I-MD-RINFO"] == "42"
    assert headers["X-Mme-Device-Id"] == SEED
    assert headers["X-Apple-I-MD-LU"] == client.local_user_id
    assert headers["X-MMe-Client-Info"] == AKD_CLIENT_INFO  # GSA 503s on the Xcode identifier
    posted = [b for m, u, _, b in client.log if u.endswith("/v3/get_headers")]
    assert posted == [{"identifier": base64.b64encode(uuid.UUID(SEED).bytes).decode(),
                       "adi_pb": base64.b64encode(b"ADI").decode()}]


def test_expired_identity_is_reprovisioned_once(client, monkeypatch):
    log = client.log
    monkeypatch.setattr(client, "_http", lambda: FakeHttp(log, headers_reply=[
        {"result": "GetHeadersError", "message": "AnisetteError -45061"},
        {"result": "Headers", "X-Apple-I-MD": "OTP2", "X-Apple-I-MD-M": "M2", "X-Apple-I-MD-RINFO": "1"},
    ]))
    client.adi_pb = b"STALE"

    headers = client.headers()

    assert headers["X-Apple-I-MD"] == "OTP2"
    assert client.adi_pb == b"ADI"
    assert len(client.sockets) == 1


def test_other_server_errors_are_not_retried(client, monkeypatch):
    monkeypatch.setattr(client, "_http", lambda: FakeHttp(client.log, headers_reply=[
        {"result": "GetHeadersError", "message": "boom"},
    ]))
    client.adi_pb = b"ADI"

    with pytest.raises(anisette.AnisetteError):
        client.headers()
    assert client.sockets == []


def test_provisioning_falls_back_to_akd_when_gsa_blocks_xcode(client, monkeypatch):
    monkeypatch.setattr(client, "_http", lambda: FakeHttp(client.log, lookup_503_for=SERVER_CLIENT_INFO))

    client.provision()

    lookups = [h["X-Mme-Client-Info"] for m, u, h, _ in client.log if u == anisette._GSA_LOOKUP_URL]
    assert lookups == [SERVER_CLIENT_INFO, AKD_CLIENT_INFO]
    assert client.adi_pb == b"ADI"


def test_gsa_client_info_drops_the_xcode_identifier():
    assert anisette.gsa_client_info(SERVER_CLIENT_INFO) == AKD_CLIENT_INFO
    assert "com.apple.dt.Xcode" not in anisette.gsa_client_info()
    assert anisette.gsa_client_info(AKD_CLIENT_INFO) == AKD_CLIENT_INFO


def test_fetch_otp_falls_through_to_v3_and_carries_its_identity(monkeypatch):
    monkeypatch.setattr(anisette, "_try_native_macos", lambda: None)
    monkeypatch.setattr(anisette, "_try_omnisette_server", lambda: None)

    class Fake:
        client_info = SERVER_CLIENT_INFO

        def headers(self):
            return {"X-Apple-I-MD": "OTP", "X-Apple-I-MD-M": "M", "X-Apple-I-MD-RINFO": "7",
                    "X-Apple-I-MD-LU": "lu", "X-Mme-Device-Id": "DEV",
                    "X-MMe-Client-Info": AKD_CLIENT_INFO}

    monkeypatch.setattr(anisette, "_v3_client", Fake())
    monkeypatch.setattr(anisette, "_anisette_v3_client", lambda: anisette._v3_client)

    http_headers = anisette.get_anisette_http_headers()
    cpd = anisette.get_anisette_headers()

    for h in (http_headers, cpd):
        assert (h["X-Apple-I-MD"], h["X-Apple-I-MD-M"]) == ("OTP", "M")
        assert h["X-Mme-Device-Id"] == "DEV" and h["X-Apple-I-MD-LU"] == "lu"
        assert h["X-Apple-I-MD-RINFO"] == "7"
    assert http_headers["X-MMe-Client-Info"] == AKD_CLIENT_INFO
    assert anisette.current_client_info() == AKD_CLIENT_INFO
    assert cpd["bootstrap"] is True


def test_fetch_otp_reports_every_source_when_all_fail(monkeypatch):
    monkeypatch.setattr(anisette, "_try_native_macos", lambda: None)
    monkeypatch.setattr(anisette, "_try_omnisette_server", lambda: None)

    def broken():
        raise RuntimeError("dns down")

    monkeypatch.setattr(anisette, "_try_anisette_v3", broken)

    with pytest.raises(anisette.AnisetteError) as excinfo:
        anisette._fetch_otp()
    assert "dns down" in str(excinfo.value)
    assert "CATAPULT_ANISETTE_SERVER" in str(excinfo.value)
