"""Watch apps inside a sideloaded IPA.

An App Store build carries its watchOS app as a placeholder under
`com.apple.WatchPlaceholder/`: an Info.plist, an asset catalog and no
executable at all. Its `WKCompanionAppBundleIdentifier` names the original app,
so once Catapult signs under a namespaced identifier installd refuses the whole
install:

    InvalidCompanionAppBundleIdentifier: The Watch app contained within this
    app has an incorrect value, "com.spotify.client", for the
    WKCompanionAppBundleIdentifier key ...

Catapult cannot provision watchOS bundles, and a placeholder has no binary to
run, so the watch app is dropped before signing.
"""

import plistlib

from catapult.signer import Signer


def _app(tmp_path, bundle_id="com.spotify.client"):
    app = tmp_path / "Spotify.app"
    app.mkdir()
    (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    return app


def _watch_placeholder(app, companion="com.spotify.client"):
    watch = app / "com.apple.WatchPlaceholder" / "WatchApp.app"
    (watch / "PlugIns" / "WatchWidgetExtension.appex").mkdir(parents=True)
    (watch / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": f"{companion}.watchkitapp",
        "WKCompanionAppBundleIdentifier": companion,
        "CFBundleExecutable": "Executable",
    }))
    (watch / "Assets.car").write_bytes(b"BOMStore")
    (watch / "PlugIns" / "WatchWidgetExtension.appex" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": f"{companion}.watchkitapp.widget"})
    )
    return watch


def _real_watch_app(app, companion="com.example.app"):
    watch = app / "Watch" / "WatchApp.app"
    watch.mkdir(parents=True)
    (watch / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": f"{companion}.watchkitapp",
        "WKCompanionAppBundleIdentifier": companion,
        "CFBundleExecutable": "WatchApp",
    }))
    (watch / "WatchApp").write_bytes(b"\xcf\xfa\xed\xfe binary")
    return watch


def test_the_watch_placeholder_is_removed(tmp_path):
    app = _app(tmp_path)
    watch = _watch_placeholder(app)

    Signer().strip_watch_apps(app)

    assert not watch.exists()
    assert not (app / "com.apple.WatchPlaceholder").exists()


def test_a_real_watch_app_is_removed_too(tmp_path):
    """Catapult provisions no watchOS App IDs, so a signed watch app would be
    rejected for the same reason a placeholder is."""
    app = _app(tmp_path, "com.example.app")
    watch = _real_watch_app(app)

    Signer().strip_watch_apps(app)

    assert not watch.exists()
    assert not (app / "Watch").exists()


def test_app_extensions_and_the_app_itself_are_left_alone(tmp_path):
    app = _app(tmp_path)
    _watch_placeholder(app)
    appex = app / "PlugIns" / "WidgetExtension.appex"
    appex.mkdir(parents=True)
    (appex / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.spotify.client.widget"})
    )

    Signer().strip_watch_apps(app)

    assert (appex / "Info.plist").exists()
    assert plistlib.loads((app / "Info.plist").read_bytes())["CFBundleIdentifier"] == "com.spotify.client"


def test_an_app_without_a_watch_app_is_untouched(tmp_path):
    app = _app(tmp_path)
    before = sorted(p.name for p in app.iterdir())

    Signer().strip_watch_apps(app)

    assert sorted(p.name for p in app.iterdir()) == before


def test_no_companion_identifier_survives_the_strip(tmp_path):
    """The regression guard: nothing left in the payload may name the original
    app as its watch companion."""
    app = _app(tmp_path)
    _watch_placeholder(app)

    Signer().strip_watch_apps(app)

    for plist in app.rglob("Info.plist"):
        assert "WKCompanionAppBundleIdentifier" not in plistlib.loads(plist.read_bytes())
