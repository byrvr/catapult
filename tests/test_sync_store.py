"""Folder store behaviour and vault state resolution.

The state table matters most. Previously an empty Keychain caused
get_sync_key() to mint a brand new key, so a second Mac silently created an
incompatible vault and uploaded into it instead of saying "locked".
"""

import json

import pytest

from catapult import sync


TEAM = "ABCDE12345"


async def test_folder_store_round_trips(tmp_path):
    store = sync.FolderStore(tmp_path / "vault")

    await store.put("teams/X/manifest.json.enc", b"payload")

    assert await store.get("teams/X/manifest.json.enc") == b"payload"
    assert await store.exists("teams/X/manifest.json.enc")


async def test_folder_store_missing_key_is_none(tmp_path):
    store = sync.FolderStore(tmp_path / "vault")

    assert await store.get("nope") is None
    assert not await store.exists("nope")


async def test_folder_store_streams_files(tmp_path):
    store = sync.FolderStore(tmp_path / "vault")
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 100_000)
    dest = tmp_path / "back.bin"

    await store.put_file("teams/X/ipas/abc.enc", src)
    await store.get_file("teams/X/ipas/abc.enc", dest)

    assert dest.read_bytes() == src.read_bytes()


async def test_folder_store_stages_outside_the_synced_root(tmp_path, monkeypatch):
    """The old code wrote <sha>.ipa.enc.tmp INSIDE the target directory, so a
    partial 500MB temp file was uploaded to iCloud and pushed to every other
    Mac before being renamed away."""
    root = tmp_path / "vault"
    store = sync.FolderStore(root)
    seen: list = []

    real_replace = sync.Path.replace

    def spy(self, target):
        seen.append((self, target))
        return real_replace(self, target)

    monkeypatch.setattr(sync.Path, "replace", spy)
    await store.put("teams/X/manifest.json.enc", b"payload")

    assert seen, "expected an atomic rename"
    staged = seen[0][0]
    assert root not in staged.parents, f"{staged} was staged inside the synced root"


async def test_folder_store_falls_back_when_rename_crosses_devices(tmp_path, monkeypatch):
    """A File Provider domain is its own mount point, so the cross-mount rename
    can fail with EXDEV. That must not break sync."""
    store = sync.FolderStore(tmp_path / "vault")
    calls = {"n": 0}
    real_replace = sync.Path.replace

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(18, "Cross-device link")
        return real_replace(self, target)

    monkeypatch.setattr(sync.Path, "replace", flaky)
    await store.put("teams/X/manifest.json.enc", b"payload")

    assert await store.get("teams/X/manifest.json.enc") == b"payload"


def test_vault_state_needs_setup_when_nothing_exists():
    assert sync.resolve_vault_state(vault_doc=None, have_key=False) == "needs_setup"


def test_vault_state_is_locked_when_remote_vault_exists_but_no_local_key():
    """The critical case. This must NOT mint a new key."""
    doc, _, _ = sync.new_vault(TEAM)

    assert sync.resolve_vault_state(vault_doc=doc, have_key=False) == "locked"


def test_vault_state_ok_when_both_present():
    doc, _, _ = sync.new_vault(TEAM)

    assert sync.resolve_vault_state(vault_doc=doc, have_key=True) == "ok"


def test_vault_state_needs_setup_when_key_is_held_but_no_remote_vault():
    """Stale local key, vault deleted remotely — offer to create, don't claim ok."""
    assert sync.resolve_vault_state(vault_doc=None, have_key=True) == "needs_setup"


def test_config_prefers_stored_json_over_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.setenv("CATAPULT_SYNC_PROVIDER", "r2")
    sync.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    sync.CONFIG_PATH.write_text(json.dumps({"provider": "folder", "folder": str(tmp_path / "v")}))

    config = sync.SyncConfig.load()

    assert config.provider == "folder"


def test_config_imports_legacy_environment_when_no_json(tmp_path, monkeypatch):
    """One release of overlap so existing users are not stranded."""
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.setenv("CATAPULT_SYNC_PROVIDER", "folder")
    monkeypatch.setenv("CATAPULT_SYNC_FOLDER", str(tmp_path / "legacy"))

    config = sync.SyncConfig.load()

    assert config.provider == "folder"
    assert config.folder == tmp_path / "legacy"


def test_config_defaults_to_disabled(tmp_path, monkeypatch):
    """A user who never opens settings must not have IPAs written into their
    iCloud Drive behind their back."""
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.delenv("CATAPULT_SYNC_PROVIDER", raising=False)
    monkeypatch.setattr(sync, "CONFIG_ENV_PATH", tmp_path / "absent.env")

    assert sync.SyncConfig.load().provider == "disabled"


def test_config_round_trips_through_save(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    config = sync.SyncConfig(provider="folder", folder=tmp_path / "vault")

    config.save()

    assert sync.SyncConfig.load().folder == tmp_path / "vault"


def test_icloud_default_path_is_the_documented_one():
    assert sync.ICLOUD_VAULT_PATH.name == "Catapult"
    assert "com~apple~CloudDocs" in str(sync.ICLOUD_VAULT_PATH)


def test_status_is_not_locked_when_a_legacy_key_is_still_configured(tmp_path, monkeypatch):
    """open_vault() adopts CATAPULT_SYNC_KEY on the next run, so reporting
    'locked' would send the user hunting for a key they do not need yet."""
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.setattr(sync, "CONFIG_ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setenv("CATAPULT_SYNC_PROVIDER", "folder")
    monkeypatch.setenv("CATAPULT_SYNC_FOLDER", str(tmp_path / "vault"))
    monkeypatch.setenv("CATAPULT_SYNC_KEY", "a" * 64)

    assert sync.status(team_id=TEAM)["vault_state"] == "ok"


def test_status_is_locked_without_any_key(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    monkeypatch.setattr(sync, "CONFIG_ENV_PATH", tmp_path / "absent.env")
    monkeypatch.setenv("CATAPULT_SYNC_PROVIDER", "folder")
    monkeypatch.setenv("CATAPULT_SYNC_FOLDER", str(tmp_path / "vault"))
    monkeypatch.delenv("CATAPULT_SYNC_KEY", raising=False)
    monkeypatch.setattr(sync, "cached_recovery_key", lambda team: None)

    assert sync.status(team_id=TEAM)["vault_state"] == "locked"
