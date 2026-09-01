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
