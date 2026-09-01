"""Daily store update checking, folded into the existing refresh loop.

Auto-update is opt-in per app, and a queued update only installs when the
device is actually reachable — nothing installs to a device that is not there.
"""

from catapult import refresh


def _record(**overrides):
    base = {
        "device_udid": "DEV1",
        "store_app_key": "github:VortXTV/VortX#tvos:",
        "store_version": "v0.3.14-beta.9",
        "store_auto_update": True,
    }
    base.update(overrides)
    return base


CATALOG = {"github:VortXTV/VortX#tvos:": "v0.3.14-beta.12"}


def test_finds_a_record_with_a_newer_release():
    state = {"installs": [_record()]}

    assert len(refresh.store_updates_due(state, CATALOG)) == 1


def test_skips_a_record_already_on_the_latest():
    state = {"installs": [_record(store_version="v0.3.14-beta.12")]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_skips_apps_that_did_not_opt_in():
    """Auto-update is off by default."""
    state = {"installs": [_record(store_auto_update=False)]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_skips_pinned_apps():
    state = {"installs": [_record(store_pinned=True)]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_ignores_manually_installed_records():
    state = {"installs": [{"device_udid": "DEV1", "ipa_path": "/tmp/x.ipa"}]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_ignores_an_app_missing_from_the_catalog():
    """The source may have removed the asset or gone offline."""
    state = {"installs": [_record(store_app_key="github:someone/gone#ios:")]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_does_not_downgrade():
    state = {"installs": [_record(store_version="v0.4.0")]}

    assert refresh.store_updates_due(state, CATALOG) == []


def test_check_is_due_when_never_run():
    assert refresh.store_check_is_due({}, now=1_000_000.0)


def test_check_is_not_due_within_a_day():
    state = {"store_checked_at": 1_000_000.0}

    assert not refresh.store_check_is_due(state, now=1_000_000.0 + 3600)


def test_check_is_due_after_a_day():
    state = {"store_checked_at": 1_000_000.0}

    assert refresh.store_check_is_due(state, now=1_000_000.0 + 86_401)


# ── wiring: the daily check inside the refresh loop ──────────────────────────

from catapult import store as _store  # noqa: E402


def _catalog_app(version="v0.3.14-beta.12"):
    return _store.StoreApp(
        source_id="github:VortXTV/VortX",
        app_key="github:VortXTV/VortX#tvos:",
        name="VortX",
        version=version,
        platform="tvos",
        download_url="https://x/vortx.ipa",
    )


class FakeDevices:
    def __init__(self, reachable):
        self.reachable = set(reachable)

    async def get_device_info(self, udid):
        if udid not in self.reachable:
            raise RuntimeError(f"{udid} is not connected")
        return {"udid": udid, "installable": True, "name": udid}


def _components(devices, installer):
    return lambda: (devices, None, None, None, None, None, installer)


async def test_daily_check_installs_updates_only_to_reachable_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [_record(device_udid="HERE"), _record(device_udid="AWAY")]})
    app = _catalog_app()
    installed = []

    async def fetch_catalog(source):
        return [app]

    async def installer(device_udid, catalog_app, progress):
        installed.append((device_udid, catalog_app.app_key))
        return {"status": "ok"}

    summary = await refresh.run_store_update_check(
        _components(FakeDevices({"HERE"}), installer),
        sources=[_store.normalize_source("VortXTV/VortX")],
        fetch_catalog=fetch_catalog,
        now=2_000_000.0,
    )

    assert installed == [("HERE", app.app_key)]
    assert summary["updated"] == ["HERE"]
    assert summary["unreachable"] == ["AWAY"]
    assert refresh.load_state()["store_checked_at"] == 2_000_000.0


async def test_daily_check_is_skipped_within_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [_record()], "store_checked_at": 2_000_000.0 - 100})
    calls = []

    async def fetch_catalog(source):
        calls.append(source.id)
        return [_catalog_app()]

    async def installer(device_udid, catalog_app, progress):
        calls.append("install")
        return {"status": "ok"}

    summary = await refresh.run_store_update_check(
        _components(FakeDevices({"DEV1"}), installer),
        sources=[_store.normalize_source("VortXTV/VortX")],
        fetch_catalog=fetch_catalog,
        now=2_000_000.0,
    )

    assert summary["status"] == "skipped"
    assert calls == []


async def test_a_failing_source_does_not_stop_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [_record()]})
    installed = []

    async def fetch_catalog(source):
        if source.id == "github:broken/repo":
            raise RuntimeError("offline")
        return [_catalog_app()]

    async def installer(device_udid, catalog_app, progress):
        installed.append(device_udid)
        return {"status": "ok"}

    summary = await refresh.run_store_update_check(
        _components(FakeDevices({"DEV1"}), installer),
        sources=[_store.normalize_source("broken/repo"), _store.normalize_source("VortXTV/VortX")],
        fetch_catalog=fetch_catalog,
        now=2_000_000.0,
    )

    assert installed == ["DEV1"]
    assert summary["source_errors"] == ["github:broken/repo"]


def test_set_store_auto_update_flags_the_matching_record(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [_record(store_auto_update=False)]})

    assert refresh.set_store_auto_update("DEV1", "github:VortXTV/VortX#tvos:", True)

    assert refresh.load_state()["installs"][0]["store_auto_update"] is True
    assert not refresh.set_store_auto_update("DEV1", "github:nobody/nothing#ios:", True)


async def test_a_check_where_every_source_failed_is_retried_next_hour(tmp_path, monkeypatch):
    """A transient outage at check time must not cost a whole day."""
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    refresh.save_state({"installs": [_record()]})

    async def fetch_catalog(source):
        raise RuntimeError("offline")

    async def installer(device_udid, catalog_app, progress):
        return {"status": "ok"}

    summary = await refresh.run_store_update_check(
        _components(FakeDevices({"DEV1"}), installer),
        sources=[_store.normalize_source("VortXTV/VortX")],
        fetch_catalog=fetch_catalog,
        now=2_000_000.0,
    )

    assert summary["source_errors"] == ["github:VortXTV/VortX"]
    assert refresh.load_state().get("store_checked_at") is None
