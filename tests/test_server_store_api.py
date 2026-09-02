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


# ── icons ──────────────────────────────────────────────────────────────────

# A signature and an IHDR chunk are all the icon reader looks at.
PNG = (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
       + (120).to_bytes(4, "big") * 2 + b"\x08\x06\x00\x00\x00" + b"\0" * 4)

GITHUB = store.normalize_source("mrdrvt99/YouProEXTRA")


def _ipa(path, members):
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


def _ipa_with_icon(path):
    return _ipa(path, {"Payload/App.app/AppIcon60x60@2x.png": PNG})


def _ipa_without_icon(path):
    return _ipa(path, {"Payload/VortX.app/Assets.car": b"BOMStore"})


@pytest.fixture
def local_dirs(monkeypatch, tmp_path):
    """Every place the icon lookup may touch on disk, pointed at tmp_path."""
    from catapult import vault

    monkeypatch.setattr(store, "ICON_CACHE_DIR", tmp_path / "icons")
    monkeypatch.setattr(store, "DOWNLOAD_CACHE_DIR", tmp_path / "downloads")
    monkeypatch.setattr(store, "icon_helper_path", lambda: None)
    monkeypatch.setattr(vault, "IPA_VAULT_DIR", tmp_path / "IPAs")
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": []})
    return tmp_path


def _counting_hasher(monkeypatch):
    from catapult import vault

    hashed = []
    real = vault.sha256_file
    monkeypatch.setattr(server._vault, "sha256_file", lambda path: hashed.append(path) or real(path))
    return hashed


def _catalog(monkeypatch, source, apps):
    monkeypatch.setattr(server._store, "load_sources", lambda: [source])

    async def fetch_catalog(src):
        return apps

    monkeypatch.setattr(server._store, "fetch_catalog", fetch_catalog)


def _entry(source=GITHUB, name="YouTubePlus", **fields):
    return store.StoreApp(source_id=source.id, app_key=f"{source.id}#{name}:unknown:", name=name,
                          version="21.24.3-5.2.2", platform="unknown",
                          download_url=f"https://x/{name}.ipa", **fields)


def _installed(monkeypatch, entry, ipa, sha):
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {"device_udid": "IPAD", "device_name": "iPad", "store_app_key": entry.app_key,
         "store_version": entry.version, "ipa_path": str(ipa), "ipa_sha256": sha},
    ]})


def _apps(client):
    return client.get("/api/store/apps").json()["apps"]


def test_icon_is_the_sources_own_url_first(client, monkeypatch, local_dirs):
    entry = _entry(icon_url="https://cdn.example/youtube.png")
    _installed(monkeypatch, entry, _ipa_with_icon(local_dirs / "a.ipa"), "a" * 64)
    _catalog(monkeypatch, GITHUB, [entry])

    (app,) = _apps(client)

    assert app["icon"] == "https://cdn.example/youtube.png"


def test_icon_comes_from_the_ipa_of_a_matched_install_record(client, monkeypatch, local_dirs):
    entry = _entry()
    _installed(monkeypatch, entry, _ipa_with_icon(local_dirs / "a.ipa"), "a" * 64)
    _catalog(monkeypatch, GITHUB, [entry])

    (app,) = _apps(client)
    served = client.get("/api/store/icon", params={"sha": "a" * 64})

    assert app["icon"] == "/api/store/icon?sha=" + "a" * 64
    assert served.status_code == 200
    assert served.content == PNG


def test_icon_comes_from_the_download_cache(client, monkeypatch, local_dirs):
    entry = _entry(sha256="c" * 64)
    ipa = _ipa_with_icon(store.download_cache_path(entry.download_url))
    entry.size = ipa.stat().st_size
    _catalog(monkeypatch, GITHUB, [entry])
    hashed = _counting_hasher(monkeypatch)

    (app,) = _apps(client)

    assert app["icon"] == "/api/store/icon?sha=" + "c" * 64
    assert hashed == [], "the published checksum names the file; no need to hash it"


def test_a_published_checksum_is_trusted_only_when_the_size_matches(client, monkeypatch, local_dirs):
    """The cache file may predate the checksum: a re-published asset, say."""
    from catapult import vault

    entry = _entry(sha256="c" * 64, size=1)
    ipa = _ipa_with_icon(store.download_cache_path(entry.download_url))
    _catalog(monkeypatch, GITHUB, [entry])

    (app,) = _apps(client)

    assert app["icon"] == f"/api/store/icon?sha={vault.sha256_file(ipa)}"


def test_the_catalog_entry_carries_the_icon_it_resolved(client, monkeypatch, local_dirs):
    entry = _entry()
    _catalog(monkeypatch, GITHUB, [entry])

    (app,) = _apps(client)

    assert entry.icon == app["icon"] == "https://github.com/mrdrvt99.png?size=128"


def test_download_cache_icon_is_keyed_by_the_files_digest_without_a_checksum(client, monkeypatch, local_dirs):
    from catapult import vault

    entry = _entry()
    ipa = _ipa_with_icon(store.download_cache_path(entry.download_url))
    _catalog(monkeypatch, GITHUB, [entry])

    (app,) = _apps(client)

    assert app["icon"] == f"/api/store/icon?sha={vault.sha256_file(ipa)}"


def test_download_cache_file_is_hashed_once_across_loads(client, monkeypatch, local_dirs):
    from catapult import vault

    entry = _entry()
    ipa = _ipa_with_icon(store.download_cache_path(entry.download_url))
    digest = vault.sha256_file(ipa)
    _catalog(monkeypatch, GITHUB, [entry])
    hashed = _counting_hasher(monkeypatch)

    first = _apps(client)
    second = _apps(client)

    assert first == second
    assert first[0]["icon"] == f"/api/store/icon?sha={digest}"
    assert len(hashed) == 1


def test_download_cache_file_is_hashed_again_once_it_changes(client, monkeypatch, local_dirs):
    import os

    entry = _entry()
    ipa = _ipa_with_icon(store.download_cache_path(entry.download_url))
    _catalog(monkeypatch, GITHUB, [entry])
    hashed = _counting_hasher(monkeypatch)

    _apps(client)
    _ipa(ipa, {"Payload/App.app/AppIcon60x60@2x.png": PNG + b"\0"})
    os.utime(ipa, ns=(0, ipa.stat().st_mtime_ns + 1_000_000_000))
    _apps(client)

    assert len(hashed) == 2


def test_download_cache_is_left_alone_once_a_matched_record_settled_the_icon(client, monkeypatch, local_dirs):
    """The record's IPA is the same build; its miss is remembered, so the
    cache file need not be hashed to learn the same thing."""
    entry = _entry()
    _installed(monkeypatch, entry, _ipa_without_icon(local_dirs / "tv.ipa"), "b" * 64)
    _ipa_with_icon(store.download_cache_path(entry.download_url))
    _catalog(monkeypatch, GITHUB, [entry])
    hashed = _counting_hasher(monkeypatch)

    first = _apps(client)
    second = _apps(client)

    assert first == second
    assert first[0]["icon"] == "https://github.com/mrdrvt99.png?size=128"
    assert hashed == []


def test_icon_falls_back_to_the_github_owner_avatar(client, monkeypatch, local_dirs):
    _catalog(monkeypatch, GITHUB, [_entry()])

    (app,) = _apps(client)

    assert app["icon"] == "https://github.com/mrdrvt99.png?size=128"


def test_icon_is_empty_for_an_altstore_app_without_one(client, monkeypatch, local_dirs):
    source = store.normalize_source("https://example.com/apps.json")
    _catalog(monkeypatch, source, [_entry(source)])

    (app,) = _apps(client)

    assert app["icon"] == ""


def test_icon_lookup_opens_each_ipa_once_across_catalog_loads(client, monkeypatch, local_dirs):
    """Both outcomes are remembered: the extracted icon and the tvOS miss."""
    youtube, vortx = _entry(), _entry(name="VortX")
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": [
        {"device_udid": "IPAD", "store_app_key": youtube.app_key,
         "ipa_path": str(_ipa_with_icon(local_dirs / "a.ipa")), "ipa_sha256": "a" * 64},
        {"device_udid": "TV1", "store_app_key": vortx.app_key,
         "ipa_path": str(_ipa_without_icon(local_dirs / "tv.ipa")), "ipa_sha256": "b" * 64},
    ]})
    _catalog(monkeypatch, GITHUB, [youtube, vortx])
    # A helper that runs and finds nothing: only a genuine miss is remembered.
    helper = local_dirs / "catapult-icon"
    helper.write_text("#!/bin/sh\nexit 1\n")
    helper.chmod(0o755)
    monkeypatch.setattr(store, "icon_helper_path", lambda: helper)
    opened = []
    real = store.icon_from_ipa
    monkeypatch.setattr(store, "icon_from_ipa", lambda path: opened.append(path) or real(path))

    first = _apps(client)
    second = _apps(client)

    assert len(opened) == 2, "one scan per IPA on the first load"
    assert [app["icon"] for app in first] == [app["icon"] for app in second]
    assert first[0]["icon"] == "/api/store/icon?sha=" + "a" * 64
    assert first[1]["icon"] == "https://github.com/mrdrvt99.png?size=128"


def test_icon_route_serves_a_cached_png_for_a_day(client, local_dirs):
    icons = local_dirs / "icons"
    icons.mkdir()
    (icons / ("e" * 64 + ".png")).write_bytes(PNG)

    response = client.get("/api/store/icon", params={"sha": "e" * 64})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "max-age=86400"
    assert response.content == PNG


def test_icon_route_404s_for_an_unknown_digest(client, local_dirs):
    assert client.get("/api/store/icon", params={"sha": "f" * 64}).status_code == 404


@pytest.mark.parametrize("sha", ["../x", "", "e" * 63, "e" * 64 + "/../../etc/passwd", "e" * 64 + "\n"])
def test_icon_route_404s_for_a_malformed_digest(client, local_dirs, sha):
    (local_dirs / "icons").mkdir()
    (local_dirs / "icons" / ("e" * 64 + ".png")).write_bytes(PNG)

    assert client.get("/api/store/icon", params={"sha": sha}).status_code == 404


def test_a_failing_icon_cache_degrades_to_the_avatar(client, monkeypatch):
    """A full disk or an unwritable Application Support must cost one row its
    real icon, not the whole Store tab."""
    _youtube_source(monkeypatch, [("YouTubePlus", "21.24.3-5.2.2", 128_944_853)])
    monkeypatch.setattr(server._refresh, "load_state", lambda: {"installs": []})

    def boom(entry, records):
        raise OSError("disk full")

    monkeypatch.setattr(server, "_local_icon_digest", boom)

    (app,) = client.get("/api/store/apps").json()["apps"]

    assert app["icon"] == "https://github.com/mrdrvt99.png?size=128"
