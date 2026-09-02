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


def _youtube_source(monkeypatch, entries):
    source = store.normalize_source("mrdrvt99/YouProEXTRA")
    monkeypatch.setattr(server._store, "load_sources", lambda: [source])

    async def fetch_catalog(src):
        return [store.StoreApp(source_id=source.id, app_key=f"{source.id}#{name}:unknown:",
                               name=name, version=version, platform="unknown",
                               download_url=f"https://x/{name}_{version.replace('-', '_')}.ipa", size=size)
                for name, version, size in entries]

    monkeypatch.setattr(server._store, "fetch_catalog", fetch_catalog)


YOUTUBE_RECORD = {"app_name": "YouTube", "source_bundle_id": "com.google.ios.youtube",
                  "app_version": "21.24.3", "ipa_sha256": "9" * 64, "ipa_size": 128_834_727,
                  "original_filename": "9" * 64 + ".ipa"}


def test_only_the_closest_entry_is_marked_when_several_tweaks_share_a_version(client, monkeypatch):
    """Four tweaks of YouTube 21.24.3 can all sit within the size tolerance of
    one hand-installed IPA. A record is one install, so it marks one entry:
    the closest in size."""
    _youtube_source(monkeypatch, [("YouTubePlus", "21.24.3-5.2.2", 128_944_853),
                                  ("YTLite", "21.24.3-5.1.0", 128_700_000),
                                  ("YouEnhanced", "21.24.3-3.0.1", 129_300_000)])
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {**YOUTUBE_RECORD, "device_udid": "IPAD", "device_name": "Ruslan's iPad"},
    ]})

    marked = {app["name"] for app in client.get("/api/store/apps").json()["apps"] if app["installed_before"]}

    assert marked == {"YouTubePlus"}


def test_installed_on_names_each_device_once_and_never_shows_udids(client, monkeypatch):
    _youtube_source(monkeypatch, [("YouTubePlus", "21.24.3-5.2.2", 128_944_853)])
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {**YOUTUBE_RECORD, "device_udid": "IPAD", "device_name": "iPad", "last_installed": 1.0},
        {**YOUTUBE_RECORD, "device_udid": "IPAD", "device_name": "Ruslan's iPad", "last_installed": 2.0},
        {**YOUTUBE_RECORD, "device_udid": "00008030-000A1B2C3D4E5F60", "device_name": ""},
    ]})

    (app,) = client.get("/api/store/apps").json()["apps"]

    assert app["installed_on"] == ["Ruslan's iPad", "another device"]


def test_catalog_backfills_versions_from_the_vault_and_persists_once(client, monkeypatch, tmp_path):
    import plistlib
    import zipfile

    from catapult import vault

    monkeypatch.setattr(vault, "IPA_VAULT_DIR", tmp_path / "IPAs")
    ipa = tmp_path / "youtube.ipa"
    with zipfile.ZipFile(ipa, "w") as z:
        z.writestr("Payload/YouTube.app/Info.plist", plistlib.dumps({"CFBundleShortVersionString": "21.24.3"}))
    vaulted = vault.store_ipa(ipa)
    record = {"device_udid": "IPAD", "device_name": "Ruslan's iPad", "app_name": "YouTube",
              "source_bundle_id": "com.google.ios.youtube", "ipa_sha256": vaulted["sha256"],
              "ipa_size": 128_834_727, "ipa_path": vaulted["path"]}
    state = {"installs": [record]}
    saved = []
    monkeypatch.setattr(server._refresh, "load_state", lambda: state)
    monkeypatch.setattr(server._refresh, "save_state", lambda s: saved.append(s))
    _youtube_source(monkeypatch, [("YouTubePlus", "21.24.3-5.2.2", 128_944_853)])

    first = client.get("/api/store/apps").json()["apps"][0]
    second = client.get("/api/store/apps").json()["apps"][0]

    assert first["installed_before"] and second["installed_before"]
    assert record["app_version"] == "21.24.3"
    assert len(saved) == 1, "backfilled once, then remembered"
