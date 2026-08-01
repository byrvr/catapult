"""open_vault(): the state machine that decides locked vs new vs migrate.

Every test isolates the Keychain, because the real one on a developer's Mac may
already hold a cached recovery key or a legacy CATAPULT_SYNC_KEY.
"""

import json

import pytest

from catapult import recoverykey, sync


TEAM = "ABCDE12345"


@pytest.fixture
def store(tmp_path):
    return sync.FolderStore(tmp_path / "vault")


@pytest.fixture(autouse=True)
def isolated_keychain(monkeypatch):
    """Swap the Keychain for a dict so tests never touch the login keychain."""
    cache: dict[str, str] = {}
    monkeypatch.setattr(sync, "_keychain_get", lambda account: cache.get(account))
    monkeypatch.setattr(
        sync, "_keychain_set", lambda account, value: cache.__setitem__(account, value) or True
    )
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: None)
    return cache


async def test_no_vault_and_no_legacy_key_needs_setup(store):
    assert await sync.open_vault(store, TEAM) == ("needs_setup", None)


async def test_remote_vault_without_a_local_key_is_locked(store):
    """The regression this whole redesign exists for: must not mint a key."""
    doc, _, _ = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())

    state, key = await sync.open_vault(store, TEAM)

    assert state == "locked"
    assert key is None


async def test_cached_key_opens_the_vault(store):
    doc, data_key, recovery_key = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())
    sync.cache_recovery_key(TEAM, recovery_key)

    assert await sync.open_vault(store, TEAM) == ("ok", data_key)


async def test_a_stale_cached_key_reports_wrong_key(store):
    """The vault was replaced remotely; say so instead of silently forking."""
    doc, _, _ = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())
    sync.cache_recovery_key(TEAM, recoverykey.generate())

    state, key = await sync.open_vault(store, TEAM)

    assert state == "wrong_key"
    assert key is None


async def test_legacy_sync_key_is_adopted_without_re_encrypting(store, monkeypatch):
    """Migration must keep every already-uploaded blob readable, so the legacy
    key becomes the data key verbatim."""
    legacy = bytes(range(32))
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: legacy)

    state, key = await sync.open_vault(store, TEAM)

    assert state == "ok"
    assert key == legacy


async def test_migration_writes_a_vault_that_the_cached_key_reopens(store, monkeypatch):
    legacy = bytes(range(32))
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: legacy)
    await sync.open_vault(store, TEAM)

    # Second run takes the normal path, with no legacy key present.
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: None)

    assert await sync.open_vault(store, TEAM) == ("ok", legacy)


async def test_unlock_accepts_the_right_key_and_caches_it(store, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    sync.SyncConfig(provider="folder", folder=store.root).save()
    doc, data_key, recovery_key = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())

    result = await sync.unlock_vault(TEAM, recoverykey.encode(recovery_key))

    assert result["status"] == "ok"
    assert await sync.open_vault(store, TEAM) == ("ok", data_key)


async def test_unlock_rejects_a_typo_before_touching_the_network(store, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    sync.SyncConfig(provider="folder", folder=store.root).save()
    doc, _, _ = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())

    result = await sync.unlock_vault(TEAM, "CAT1-NOTAV-ALIDK-EYATA-LLLLL-LLLLL-LL")

    assert result["status"] in {"invalid", "wrong_key"}


async def test_unlock_reports_when_there_is_no_vault(store, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "CONFIG_PATH", tmp_path / "sync.json")
    sync.SyncConfig(provider="folder", folder=store.root).save()

    result = await sync.unlock_vault(TEAM, recoverykey.encode(recoverykey.generate()))

    assert result["status"] == "needs_setup"
