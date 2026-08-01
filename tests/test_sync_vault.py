"""Vault envelope encryption and the streaming blob format.

Two problems this replaces:

1. get_sync_key() minted a fresh random key when the Keychain had none, so a
   second Mac silently created an incompatible vault instead of reporting a
   locked one. _normalize_key() also fell back to a bare unsalted sha256 of
   whatever string the user supplied.

2. Blobs were encrypted whole in memory: _encrypt_bytes(key, path.read_bytes())
   needs roughly 3 GB of RSS for a 1 GB IPA.
"""

import pytest

from catapult import sync


TEAM = "ABCDE12345"


def test_new_vault_produces_a_recoverable_data_key():
    doc, data_key, recovery_key = sync.new_vault(TEAM)

    assert len(data_key) == 32
    assert len(recovery_key) == 16
    assert sync.unwrap_data_key(doc, recovery_key, TEAM) == data_key


def test_vault_document_carries_no_secrets():
    doc, data_key, recovery_key = sync.new_vault(TEAM)
    blob = repr(doc).encode()

    assert data_key not in blob
    assert recovery_key not in blob
    assert doc["vault_format"] == sync.VAULT_FORMAT


def test_wrong_recovery_key_is_reported_distinctly():
    doc, _, _ = sync.new_vault(TEAM)
    other = sync.recoverykey.generate()

    with pytest.raises(sync.WrongRecoveryKey):
        sync.unwrap_data_key(doc, other, TEAM)


def test_vault_is_bound_to_its_team():
    """team_id is the HKDF salt and the AEAD aad, so a key for one team's vault
    cannot silently open another's."""
    doc, _, recovery_key = sync.new_vault(TEAM)

    with pytest.raises(sync.WrongRecoveryKey):
        sync.unwrap_data_key(doc, recovery_key, "OTHERTEAM1")


def test_unwrap_rejects_a_corrupted_document():
    doc, _, recovery_key = sync.new_vault(TEAM)
    doc["wrap"]["ct"] = doc["wrap"]["ct"][:-4] + "AAAA"

    with pytest.raises(sync.WrongRecoveryKey):
        sync.unwrap_data_key(doc, recovery_key, TEAM)


@pytest.mark.parametrize("size", [0, 1, 1024, sync.CHUNK_SIZE, sync.CHUNK_SIZE * 2 + 7])
def test_stream_round_trip(tmp_path, size):
    key = sync.new_data_key()
    payload = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
    src = tmp_path / "in.ipa"
    src.write_bytes(payload)
    enc = tmp_path / "out.enc"
    dec = tmp_path / "back.ipa"

    sync.encrypt_file(key, src, enc)
    sync.decrypt_file(key, enc, dec)

    assert dec.read_bytes() == payload


def test_stream_uses_the_v2_container(tmp_path):
    key = sync.new_data_key()
    src = tmp_path / "in.ipa"
    src.write_bytes(b"hello")
    enc = tmp_path / "out.enc"

    sync.encrypt_file(key, src, enc)

    assert enc.read_bytes().startswith(sync.V2_MAGIC)


def test_stream_rejects_the_wrong_key(tmp_path):
    src = tmp_path / "in.ipa"
    src.write_bytes(b"hello")
    enc = tmp_path / "out.enc"
    sync.encrypt_file(sync.new_data_key(), src, enc)

    with pytest.raises(Exception):
        sync.decrypt_file(sync.new_data_key(), enc, tmp_path / "back.ipa")


def test_stream_detects_truncation(tmp_path):
    """A partial upload must fail loudly, not yield a short IPA."""
    key = sync.new_data_key()
    src = tmp_path / "in.ipa"
    src.write_bytes(b"x" * (sync.CHUNK_SIZE * 2))
    enc = tmp_path / "out.enc"
    sync.encrypt_file(key, src, enc)

    truncated = tmp_path / "cut.enc"
    data = enc.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])

    with pytest.raises(Exception):
        sync.decrypt_file(key, truncated, tmp_path / "back.ipa")


def test_stream_detects_a_flipped_byte(tmp_path):
    key = sync.new_data_key()
    src = tmp_path / "in.ipa"
    src.write_bytes(b"y" * 4096)
    enc = tmp_path / "out.enc"
    sync.encrypt_file(key, src, enc)

    data = bytearray(enc.read_bytes())
    data[-1] ^= 0xFF
    enc.write_bytes(bytes(data))

    with pytest.raises(Exception):
        sync.decrypt_file(key, enc, tmp_path / "back.ipa")


def test_still_reads_v1_blobs(tmp_path):
    """Existing remote vaults must keep working with no migration."""
    key = sync.new_data_key()
    payload = b"a legacy blob"
    legacy = sync._encrypt_bytes(key, payload)
    enc = tmp_path / "legacy.enc"
    enc.write_bytes(legacy)
    dec = tmp_path / "back.bin"

    sync.decrypt_file(key, enc, dec)

    assert dec.read_bytes() == payload


def test_decrypt_file_leaves_no_partial_output_on_failure(tmp_path):
    key = sync.new_data_key()
    src = tmp_path / "in.ipa"
    src.write_bytes(b"z" * 4096)
    enc = tmp_path / "out.enc"
    sync.encrypt_file(key, src, enc)
    dest = tmp_path / "back.ipa"

    with pytest.raises(Exception):
        sync.decrypt_file(sync.new_data_key(), enc, dest)

    assert not dest.exists()
