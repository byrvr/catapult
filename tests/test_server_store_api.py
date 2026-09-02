"""Store endpoints: the per-app auto-update flag round-trips to the UI."""

import pytest
from fastapi.testclient import TestClient

from catapult import server, store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server.auth_client, "session", None, raising=False)
    return TestClient(server.app)


def _app():
    return store.StoreApp(
        source_id="github:VortXTV/VortX",
        app_key="github:VortXTV/VortX#tvos:",
        name="VortX",
        version="v0.3.14-beta.12",
        platform="tvos",
        download_url="https://x/vortx.ipa",
    )


def test_catalog_reports_the_auto_update_flag_for_the_selected_device(client, monkeypatch):
    monkeypatch.setattr(server._store, "load_sources", lambda: [store.normalize_source("VortXTV/VortX")])

    async def fetch_catalog(source):
        return [_app()]

    async def device_info(udid):
        return {"udid": udid, "device_class": "tvos"}

    monkeypatch.setattr(server._store, "fetch_catalog", fetch_catalog)
    monkeypatch.setattr(server.device_manager, "get_device_info", device_info)
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {"device_udid": "TV1", "store_app_key": "github:VortXTV/VortX#tvos:",
         "store_version": "v0.3.14-beta.9", "store_auto_update": True},
        {"device_udid": "TV2", "store_app_key": "github:VortXTV/VortX#tvos:",
         "store_version": "v0.3.14-beta.9"},
    ]})

    body = client.get("/api/store/apps", params={"device_udid": "TV1"}).json()
    assert body["apps"][0]["auto_update"] is True
    assert body["apps"][0]["installed_version"] == "v0.3.14-beta.9"

    body = client.get("/api/store/apps", params={"device_udid": "TV2"}).json()
    assert body["apps"][0]["auto_update"] is False


def test_auto_update_toggle_updates_the_record(client, monkeypatch):
    seen = {}

    def set_flag(device_udid, app_key, enabled):
        seen.update(device_udid=device_udid, app_key=app_key, enabled=enabled)
        return True

    monkeypatch.setattr(server._refresh, "set_store_auto_update", set_flag)

    response = client.post(
        "/api/store/apps/auto-update",
        json={"device_udid": "TV1", "app_key": "github:VortXTV/VortX#tvos:", "enabled": True},
    )

    assert response.status_code == 200
    assert seen == {"device_udid": "TV1", "app_key": "github:VortXTV/VortX#tvos:", "enabled": True}


def test_auto_update_toggle_404s_for_an_unknown_install(client, monkeypatch):
    monkeypatch.setattr(server._refresh, "set_store_auto_update", lambda *a: False)

    response = client.post(
        "/api/store/apps/auto-update",
        json={"device_udid": "TV1", "app_key": "nope", "enabled": True},
    )

    assert response.status_code == 404


def test_catalog_marks_apps_installed_before_across_devices(client, monkeypatch):
    """A YouTube tweak installed by hand before the Store existed has no store
    link, but the vault knows its version and size. Only the matching entry is
    marked, and it says which devices it went to."""
    source = store.normalize_source("mrdrvt99/YouProEXTRA")
    monkeypatch.setattr(server._store, "load_sources", lambda: [source])

    def entry(name, version, size):
        return store.StoreApp(source_id=source.id, app_key=f"{source.id}#{name}:unknown:",
                              name=name, version=version, platform="unknown",
                              download_url=f"https://x/{name}.ipa", size=size)

    async def fetch_catalog(src):
        return [entry("YouTubePlus", "21.24.3-5.2.2", 128_944_853),
                entry("YouProExtra", "21.24.3-1.3.1", 119_894_440),
                entry("YouMod", "21.35.3-2.0.0", 128_284_527)]

    record = {"app_name": "YouTube", "source_bundle_id": "com.google.ios.youtube",
              "app_version": "21.24.3", "ipa_sha256": "9" * 64, "ipa_size": 128_834_727}
    monkeypatch.setattr(server._store, "fetch_catalog", fetch_catalog)
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {**record, "device_udid": "IPAD", "device_name": "Ruslan's iPad"},
        {**record, "device_udid": "IPHONE", "device_name": "Ruslan's iPhone"},
    ]})

    body = client.get("/api/store/apps").json()
    by_name = {app["name"]: app for app in body["apps"]}

    assert by_name["YouTubePlus"]["installed_before"] is True
    assert by_name["YouTubePlus"]["installed_on"] == ["Ruslan's iPad", "Ruslan's iPhone"]
    assert by_name["YouProExtra"]["installed_before"] is False
    assert by_name["YouMod"]["installed_before"] is False
