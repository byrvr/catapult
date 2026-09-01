"""sync_state() end to end: Mac #1 uploads, Mac #2 downloads, nothing leaks.

The upload/download path had no coverage at all. This drives the real
FolderStore, the real streaming blob format, and the real local IPA vault,
with only the Keychain and the on-disk locations pointed at temp directories.
"""

import json
import shutil
from types import SimpleNamespace

import pytest

from catapult import refresh, sync, vault

TEAM = "ABCDE12345"
APPLE_ID = "me@example.com"


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch):
    cache: dict[str, str] = {}
    monkeypatch.setattr(sync, "_keychain_get", lambda account: cache.get(account))
    monkeypatch.setattr(
        sync, "_keychain_set", lambda account, value: cache.__setitem__(account, value) or True
    )
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: None)
    return cache


@pytest.fixture
def remote(tmp_path):
    return sync.FolderStore(tmp_path / "remote")


def _become_mac(monkeypatch, root, store, *, leaving=None):
    """Point install state, the IPA vault, and the sync config at one Mac.

    ``leaving`` is the previous Mac's root: its IPA vault is removed so the new
    Mac cannot see files through the other Mac's absolute paths, which only
    works here because both "Macs" share one filesystem.
    """
    if leaving is not None:
        shutil.rmtree(leaving / "IPAs", ignore_errors=True)
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(refresh, "STATE_FILE", root / "state.json")
    monkeypatch.setattr(vault, "IPA_VAULT_DIR", root / "IPAs")
    config = SimpleNamespace(
        provider="folder", folder=store.root, configured=True, r2_endpoint="", r2_bucket=""
    )
    monkeypatch.setattr(sync.SyncConfig, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(sync, "_store_from_config", lambda cfg: store)


async def _first_mac_with_one_install(tmp_path, monkeypatch, remote):
    _become_mac(monkeypatch, tmp_path / "mac1", remote)
    doc, _, recovery_key = sync.new_vault(TEAM)
    await remote.put(sync._vault_key(TEAM), json.dumps(doc).encode())
    sync.cache_recovery_key(TEAM, recovery_key)

    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK\x03\x04" + b"a" * 50_000)
    vaulted = vault.store_ipa(ipa)
    refresh.save_state({"installs": [{
        "device_udid": "TV1", "device_name": "Living Room",
        "ipa_path": vaulted["path"], "ipa_sha256": vaulted["sha256"],
        "bundle_id": "com.example.app", "app_name": "App", "last_installed": 1_000.0,
    }]})
    return ipa, vaulted


async def test_first_mac_uploads_and_second_mac_downloads(tmp_path, monkeypatch, remote):
    ipa, vaulted = await _first_mac_with_one_install(tmp_path, monkeypatch, remote)

    first = await sync.sync_state(APPLE_ID, TEAM)

    assert first["status"] == "ok"
    assert first["uploaded_ipas"] == 1
    blob_key = sync._ipa_key(TEAM, vaulted["sha256"])
    assert await remote.exists(blob_key)
    # Nothing on the remote is plaintext.
    assert b"a" * 1000 not in await remote.get(blob_key)
    assert b"com.example.app" not in await remote.get(sync._manifest_key(TEAM))

    # Mac #2: empty state and IPA vault, same recovery key entered.
    _become_mac(monkeypatch, tmp_path / "mac2", remote, leaving=tmp_path / "mac1")
    refresh.save_state({"installs": []})

    second = await sync.sync_state(APPLE_ID, TEAM)

    assert second["status"] == "ok"
    assert second["downloaded_ipas"] == 1
    downloaded = vault.vault_path(vaulted["sha256"])
    assert downloaded.read_bytes() == ipa.read_bytes()
    (record,) = refresh.load_state()["installs"]
    assert record["bundle_id"] == "com.example.app"
    assert record["ipa_path"] == str(downloaded)


async def test_a_second_sync_uploads_nothing_new(tmp_path, monkeypatch, remote):
    await _first_mac_with_one_install(tmp_path, monkeypatch, remote)
    await sync.sync_state(APPLE_ID, TEAM)

    again = await sync.sync_state(APPLE_ID, TEAM)

    assert again["uploaded_ipas"] == 0
    assert again["downloaded_ipas"] == 0


async def test_a_tampered_blob_never_reaches_the_local_vault(tmp_path, monkeypatch, remote):
    _, vaulted = await _first_mac_with_one_install(tmp_path, monkeypatch, remote)
    await sync.sync_state(APPLE_ID, TEAM)
    blob_key = sync._ipa_key(TEAM, vaulted["sha256"])
    blob = bytearray(await remote.get(blob_key))
    blob[len(blob) // 2] ^= 0xFF
    await remote.put(blob_key, bytes(blob))

    _become_mac(monkeypatch, tmp_path / "mac2", remote, leaving=tmp_path / "mac1")
    refresh.save_state({"installs": []})

    with pytest.raises(Exception):
        await sync.sync_state(APPLE_ID, TEAM)

    assert not vault.vault_path(vaulted["sha256"]).exists()
