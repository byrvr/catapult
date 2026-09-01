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


async def test_legacy_key_opens_a_vault_another_mac_migrated(store, monkeypatch):
    """Two Macs shared CATAPULT_SYNC_KEY. Mac #1 migrated it into a vault; Mac #2
    still only has the legacy key. That key IS the data key, so Mac #2 must not
    be reported locked with no recovery key to enter."""
    legacy = b"L" * 32
    monkeypatch.setattr(sync, "legacy_sync_key", lambda: legacy)
    recovery_key = recoverykey.generate()
    doc = {
        "vault_format": sync.VAULT_FORMAT,
        "team_id": TEAM,
        "migrated_from": "CATAPULT_SYNC_KEY",
        "wrap": sync.wrap_data_key(legacy, recovery_key, TEAM),
    }
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())

    assert await sync.open_vault(store, TEAM) == ("ok", legacy)


@pytest.fixture
def configured(store, monkeypatch):
    """create_vault() reads the config only to find its store."""
    monkeypatch.setattr(sync.SyncConfig, "load", classmethod(lambda cls: object()))
    monkeypatch.setattr(sync, "_store_from_config", lambda config: store)
    return store


async def test_create_vault_refuses_to_overwrite_an_existing_vault(configured):
    """The descriptor holds the only wrap of the data key. One unconfirmed click
    used to overwrite it, lock every other Mac out, and leave the old manifest
    undecryptable so every later sync reported wrong_key."""
    first = await sync.create_vault("me@example.com", TEAM)
    assert first["status"] == "ok"
    await configured.put(sync._manifest_key(TEAM), b"manifest-under-the-first-key")

    second = await sync.create_vault("me@example.com", TEAM)

    assert second["status"] == "exists"
    assert await configured.get(sync._manifest_key(TEAM)) == b"manifest-under-the-first-key"


async def test_replacing_a_vault_keeps_the_old_one_aside(configured):
    first = await sync.create_vault("me@example.com", TEAM)
    await configured.put(sync._manifest_key(TEAM), b"old-manifest")
    await configured.put(sync._ipa_key(TEAM, "ab" * 32), b"old-blob")

    replaced = await sync.create_vault("me@example.com", TEAM, replace=True)

    assert replaced["status"] == "ok"
    assert replaced["recovery_key"] != first["recovery_key"]
    # The new vault starts empty, so the next sync re-uploads from the local vault...
    assert await configured.get(sync._manifest_key(TEAM)) is None
    assert not await configured.exists(sync._ipa_key(TEAM, "ab" * 32))
    # ...and nothing was destroyed.
    kept = [p for p in (configured.root / "teams").iterdir() if p.name.startswith(f"{TEAM}.replaced-")]
    assert len(kept) == 1
    assert (kept[0] / "manifest.json.enc").read_bytes() == b"old-manifest"
    # This Mac holds the new key and can open the new vault.
    state, _ = await sync.open_vault(configured, TEAM)
    assert state == "ok"


@pytest.fixture
def folder_config(store, monkeypatch):
    from types import SimpleNamespace

    config = SimpleNamespace(
        provider="folder", folder=store.root, configured=True, r2_endpoint="", r2_bucket=""
    )
    monkeypatch.setattr(sync.SyncConfig, "load", classmethod(lambda cls: config))
    return config


async def test_status_reports_needs_setup_before_any_vault_exists(store, folder_config):
    """Mac #1's first run. The pane used to say "This vault is locked, paste the
    key from your other Mac" and hide the Create vault button."""
    assert sync.status("me@example.com", TEAM)["vault_state"] == "needs_setup"


async def test_status_reports_locked_when_the_vault_exists_without_a_key(store, folder_config):
    doc, _, _ = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())

    assert sync.status("me@example.com", TEAM)["vault_state"] == "locked"


async def test_status_reports_ok_once_the_key_is_cached(store, folder_config):
    doc, _, recovery_key = sync.new_vault(TEAM)
    await store.put(sync._vault_key(TEAM), json.dumps(doc).encode())
    sync.cache_recovery_key(TEAM, recovery_key)

    assert sync.status("me@example.com", TEAM)["vault_state"] == "ok"
