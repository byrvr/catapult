"""_run_refresh_cycle honours the refresh lease when sync is configured.

Two Macs sharing a vault must not refresh the same apps at once. The lease is
acquired for the cycle and released afterwards; when sync is off there is no
lease to take and the cycle runs as before.
"""

from catapult import refresh, sync

TEAM = "ABCDE12345"
RECORD = {"device_udid": "D", "ipa_path": "/x.ipa", "app_name": "App"}


class _Session:
    authenticated = True
    apple_id = "me@example.com"


def _components():
    auth = type("Auth", (), {"session": _Session()})()
    return (object(), auth, object(), object(), object(), None)


def _record_refreshes(monkeypatch):
    refreshed = []

    async def fake_refresh(rec, *components):
        refreshed.append(rec)
        return {"status": "ok"}

    monkeypatch.setattr(refresh, "_refresh_install", fake_refresh)
    return refreshed


async def test_cycle_is_skipped_while_another_mac_holds_the_lease(tmp_path, monkeypatch):
    store = sync.FolderStore(tmp_path / "vault")
    await sync.acquire_refresh_lease(store, TEAM, machine_id="other-mac")

    async def lease_context(dev_services, session):
        return store, TEAM, "this-mac"

    monkeypatch.setattr(refresh, "_refresh_lease_context", lease_context)
    refreshed = _record_refreshes(monkeypatch)

    await refresh._run_refresh_cycle([dict(RECORD)], _components)

    assert refreshed == []


async def test_cycle_takes_and_releases_the_lease(tmp_path, monkeypatch):
    store = sync.FolderStore(tmp_path / "vault")

    async def lease_context(dev_services, session):
        return store, TEAM, "this-mac"

    monkeypatch.setattr(refresh, "_refresh_lease_context", lease_context)
    refreshed = _record_refreshes(monkeypatch)

    await refresh._run_refresh_cycle([dict(RECORD)], _components)

    assert len(refreshed) == 1
    assert not await store.exists(sync._lease_key(TEAM))


async def test_cycle_runs_without_a_lease_when_sync_is_off(monkeypatch):
    async def lease_context(dev_services, session):
        return None

    monkeypatch.setattr(refresh, "_refresh_lease_context", lease_context)
    refreshed = _record_refreshes(monkeypatch)

    await refresh._run_refresh_cycle([dict(RECORD)], _components)

    assert len(refreshed) == 1


async def test_store_check_defers_while_another_mac_holds_the_lease(tmp_path, monkeypatch):
    """Two Macs sharing a vault must not both install the same store update to
    the same device. The daily check runs under the refresh lease as well."""
    from catapult import store as _store

    remote = sync.FolderStore(tmp_path / "vault")
    await sync.acquire_refresh_lease(remote, TEAM, machine_id="other-mac")

    async def lease_context(dev_services, session):
        return remote, TEAM, "this-mac"

    monkeypatch.setattr(refresh, "_refresh_lease_context", lease_context)
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [{
        "device_udid": "TV1", "store_app_key": "github:o/r#tvos:",
        "store_version": "v1.0", "store_auto_update": True,
    }]})
    app = _store.StoreApp(source_id="github:o/r", app_key="github:o/r#tvos:", name="A",
                          version="v1.1", platform="tvos", download_url="https://x/a.ipa")
    installed = []

    async def fetch_catalog(source):
        return [app]

    async def installer(device_udid, catalog_app, progress):
        installed.append(device_udid)
        return {"status": "ok"}

    class Devices:
        async def get_device_info(self, udid):
            return {"udid": udid, "installable": True}

    summary = await refresh.run_store_update_check(
        lambda: (Devices(), _components()[1], object(), None, None, None, installer),
        sources=[_store.normalize_source("o/r")],
        fetch_catalog=fetch_catalog,
        now=2_000_000.0,
    )

    assert installed == []
    assert summary["status"] == "skipped"
    # Not stamped: the next hourly tick tries again instead of waiting a day.
    assert refresh.load_state().get("store_checked_at") is None


async def test_lease_context_is_none_when_icloud_drive_is_off(tmp_path, monkeypatch):
    """Writing the lease into a missing iCloud folder recreates the folder, after
    which iCloud Drive looks 'available' forever and the needs_icloud state can
    never be reported. When the configured folder is not there, there is no
    store to lease against."""
    from types import SimpleNamespace

    missing = tmp_path / "Mobile Documents" / "com~apple~CloudDocs" / "Catapult"
    monkeypatch.setattr(sync, "ICLOUD_VAULT_PATH", missing)
    monkeypatch.setattr(sync, "icloud_drive_available", lambda: False)
    config = SimpleNamespace(provider="folder", folder=missing, configured=True,
                             r2_endpoint="", r2_bucket="")
    monkeypatch.setattr(sync.SyncConfig, "load", classmethod(lambda cls: config))

    class Services:
        async def get_team(self, session):
            return {"teamId": TEAM}

    assert await refresh._refresh_lease_context(Services(), _Session()) is None
    assert not missing.exists()
