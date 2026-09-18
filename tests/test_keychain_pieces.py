"""Keychain storage of values longer than one ``security -i`` line.

``security -i`` reads each command into a 4096-byte buffer. A signing
certificate plus its private key is 5-6 KB, 11-12 KB once hex-encoded, so the
old single-line write stored the first ~2 KB and failed on the rest — every
signing call then found the identity unreadable and minted a new certificate.
Here ``security`` is emulated with exactly that line buffer.
"""

import base64
import datetime as dt
import json
import subprocess

import pytest

from catapult import refresh, signing_identity

LINE_BUFFER = 4096


class FakeSecurity:
    """Stand-in for the ``security`` CLI as ``refresh`` drives it."""

    def __init__(self):
        self.items: dict[str, str] = {}
        self.commands: list[list[str]] = []

    def run(self, argv, input=None, capture_output=False, text=False, **kw):
        self.commands.append(list(argv))
        assert argv[0] == "security"
        if argv[1] == "-i":
            return self._interactive(input)
        if argv[1] == "find-generic-password":
            account = argv[argv.index("-a") + 1]
            if account in self.items:
                return subprocess.CompletedProcess(argv, 0, stdout=self.items[account] + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 44, stdout="", stderr="could not be found")
        if argv[1] == "delete-generic-password":
            account = argv[argv.index("-a") + 1]
            if self.items.pop(account, None) is None:
                return subprocess.CompletedProcess(argv, 44, stdout=b"", stderr=b"")
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected security call {argv}")

    def _interactive(self, script: str):
        # One command per line, each cut at the line buffer like the real binary.
        status = 0
        raw = script.encode("utf-8")
        for line in raw.split(b"\n"):
            if not line:
                continue
            for start in range(0, len(line), LINE_BUFFER):
                status |= self._command(line[start:start + LINE_BUFFER].decode("utf-8", "replace"))
        return subprocess.CompletedProcess(["security", "-i"], status, stdout="", stderr="")

    def _command(self, line: str) -> int:
        tokens = line.split()
        if not tokens or tokens[0] != "add-generic-password":
            return 1
        try:
            account = tokens[tokens.index("-a") + 1].strip("'\"")
            value = bytes.fromhex(tokens[tokens.index("-X") + 1]).decode("utf-8", "replace")
        except (ValueError, IndexError):
            return 1
        if account in self.items and "-U" not in tokens:
            return 45  # "The specified item already exists in the keychain."
        self.items[account] = value
        return 0


@pytest.fixture
def security(monkeypatch):
    fake = FakeSecurity()
    monkeypatch.setattr(refresh.subprocess, "run", fake.run)
    return fake


def test_short_values_stay_inline_and_unchanged(security):
    tokens = json.dumps({"adsid": "x" * 40, "gs_token": "y" * 400})

    assert refresh._keychain_set("user@example.com", tokens)
    assert security.items == {"user@example.com": tokens}
    assert refresh._keychain_get("user@example.com") == tokens


def test_a_value_longer_than_one_line_is_lost_by_a_single_write(security):
    """The behaviour being fixed: a plain write silently keeps ~2 KB."""
    big = json.dumps({"cert_pem": "A" * 6000})

    assert not refresh._keychain_put("signing-cert:T", big)
    stored = security.items["signing-cert:T"]
    assert 0 < len(stored) < len(big)
    with pytest.raises(json.JSONDecodeError):
        json.loads(stored)


def test_long_values_round_trip_through_pieces(security):
    big = json.dumps({"cert_pem": "A" * 3000, "key_pem": "B" * 2500, "certificate_id": "ID"})

    assert refresh._keychain_set("signing-cert:T", big)

    assert refresh._keychain_get("signing-cert:T") == big
    pointer = security.items["signing-cert:T"]
    count = int(pointer.removeprefix(refresh._KEYCHAIN_PIECES_MARKER))
    assert count == -(-len(base64.b64encode(big.encode())) // refresh._KEYCHAIN_PIECE_CHARS)
    assert set(security.items) == {"signing-cert:T", *(f"signing-cert:T#{i}" for i in range(count))}
    assert all(len(item) <= refresh._KEYCHAIN_PIECE_CHARS for k, item in security.items.items() if "#" in k)


def test_rewriting_with_a_shorter_value_drops_stale_pieces(security):
    big = json.dumps({"cert_pem": "A" * 9000})
    smaller = json.dumps({"cert_pem": "A" * 2500})
    assert refresh._keychain_set("signing-cert:T", big)
    pieces_before = sum("#" in k for k in security.items)

    assert refresh._keychain_set("signing-cert:T", smaller)

    assert refresh._keychain_get("signing-cert:T") == smaller
    assert sum("#" in k for k in security.items) < pieces_before
    assert refresh._keychain_set("signing-cert:T", "tiny")
    assert security.items == {"signing-cert:T": "tiny"}


def test_delete_removes_the_pieces_too(security):
    assert refresh._keychain_set("signing-cert:T", "Z" * 5000)

    refresh._keychain_delete("signing-cert:T")

    assert security.items == {}
    assert refresh._keychain_get("signing-cert:T") is None


def test_a_missing_or_corrupt_piece_reads_as_absent(security):
    assert refresh._keychain_set("signing-cert:T", "Z" * 5000)
    security.items["signing-cert:T#1"] = "not base64 !!"
    assert refresh._keychain_get("signing-cert:T") is None

    del security.items["signing-cert:T#1"]
    assert refresh._keychain_get("signing-cert:T") is None


def test_signing_identity_survives_the_keychain_round_trip(security, tmp_path, monkeypatch):
    """End to end: the identity that used to come back truncated now loads."""
    from test_signing_identity import _make_cert, _own_identity

    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    cert_pem, key = _make_cert(dt.datetime(2027, 6, 1))
    identity = _own_identity(cert_pem, key, certificate_id="ABC123", serial_number="1")
    assert len(identity.to_json()) > refresh._KEYCHAIN_INLINE_LIMIT

    signing_identity.save("TEAM", identity)

    loaded = signing_identity.load("TEAM")
    assert loaded is not None and loaded.certificate_id == "ABC123"
    assert loaded.cert_pem == cert_pem
    assert not (tmp_path / "signing-identity-TEAM.json").exists()


def test_a_truncated_pre_0_4_2_keychain_copy_yields_to_the_file(security, tmp_path, monkeypatch):
    from test_signing_identity import _make_cert, _own_identity

    monkeypatch.setattr(signing_identity, "FALLBACK_DIR", tmp_path)
    cert_pem, key = _make_cert(dt.datetime(2027, 6, 1))
    identity = _own_identity(cert_pem, key, certificate_id="FILE1", serial_number="1")
    security.items["signing-cert:TEAM"] = identity.to_json()[:2009]  # what 0.4.1 left behind
    (tmp_path / "signing-identity-TEAM.json").write_text(identity.to_json())

    loaded = signing_identity.load("TEAM")

    assert loaded is not None and loaded.certificate_id == "FILE1"
