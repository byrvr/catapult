"""Store icons: pulling an app icon out of an IPA and remembering the result.

The PNGs here are hand-made: a signature and an IHDR chunk are all the width
reader needs, and Apple's pngcrush output puts a CgBI chunk in front of IHDR,
so that shape is covered too.
"""

import plistlib
import struct
import zipfile
from pathlib import Path

import pytest

from catapult import store


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + b"\0\0\0\0"


def png(width: int, *, padding: int = 0, cgbi: bool = False) -> bytes:
    ihdr = _chunk(b"IHDR", struct.pack(">II", width, width) + b"\x08\x06\x00\x00\x00")
    lead = _chunk(b"CgBI", b"\x50\x00\x20\x06") if cgbi else b""
    return PNG_SIGNATURE + lead + ihdr + b"\0" * padding


def make_ipa(path, members: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


@pytest.fixture
def icon_dir(monkeypatch, tmp_path):
    icons = tmp_path / "icons"
    monkeypatch.setattr(store, "ICON_CACHE_DIR", icons)
    return icons


@pytest.fixture(autouse=True)
def no_installed_helper(monkeypatch):
    """Only a helper a test points at explicitly, never the Mac's own."""
    monkeypatch.delenv("CATAPULT_ICON_HELPER", raising=False)
    monkeypatch.setattr(store, "ICON_HELPER_PATHS", ())


def info_plist(primary: dict | None = None, ipad: dict | None = None) -> bytes:
    info = {"CFBundleIdentifier": "com.google.ios.youtube"}
    if primary is not None:
        info["CFBundleIcons"] = {"CFBundlePrimaryIcon": primary}
    if ipad is not None:
        info["CFBundleIcons~ipad"] = {"CFBundlePrimaryIcon": ipad}
    return plistlib.dumps(info, fmt=plistlib.FMT_BINARY)


YOUTUBE_ICON = "logo_youtube_2024_q4_color"


def catalog_ipa(path, plist: bytes = b""):
    """An IPA in the modern shape: the icon lives in Assets.car only."""
    members = {"Payload/YouTube.app/Assets.car": b"BOMStore"}
    if plist:
        members["Payload/YouTube.app/Info.plist"] = plist
    return make_ipa(path, members)


def fake_helper(tmp_path, monkeypatch, *, succeed_for: str | None = "*"):
    """A stand-in catapult-icon: logs its arguments, writes a PNG when told to.

    Returns the log; each line is the icon name of one invocation.
    """
    log = tmp_path / "helper.log"
    fixed = tmp_path / "fixed.png"
    fixed.write_bytes(png(1024))
    if succeed_for is None:
        write = "exit 1"
    else:
        guard = "" if succeed_for == "*" else f'[ "$2" = "{succeed_for}" ] || exit 1\n'
        write = f'{guard}cp "{fixed}" "$3"'
    script = tmp_path / "catapult-icon"
    script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$2" >> "{log}"\n{write}\n')
    script.chmod(0o755)
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(script))
    return log


def helper_calls(log) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ── icon_from_ipa ──────────────────────────────────────────────────────────

def test_picks_the_largest_icon_by_ihdr_width(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/YouTube.app/AppIcon60x60@2x.png": png(120, padding=500),
        "Payload/YouTube.app/AppIcon76x76@2x~ipad.png": png(152),
        "Payload/YouTube.app/Info.plist": b"<plist/>",
    })

    assert store.icon_from_ipa(ipa) == png(152)


def test_file_size_breaks_a_width_tie(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/AppIcon60x60@2x.png": png(120),
        "Payload/App.app/AppIcon60x60@2x~ipad.png": png(120, padding=64),
    })

    assert store.icon_from_ipa(ipa) == png(120, padding=64)


def test_reads_the_width_past_apples_cgbi_chunk(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/AppIcon60x60@2x.png": png(120, padding=900),
        "Payload/App.app/AppIcon76x76@2x~ipad.png": png(152, cgbi=True),
    })

    assert store.icon_from_ipa(ipa) == png(152, cgbi=True)


def test_ignores_icons_outside_the_app_root(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/AppIcon60x60@2x.png": png(120),
        "Payload/App.app/PlugIns/Widget.appex/AppIcon76x76@2x.png": png(1024),
        "Payload/App.app/Watch/App.app/AppIcon100x100.png": png(1024),
        "Payload/App.app/Frameworks/Kit.framework/AppIcon.png": png(1024),
    })

    assert store.icon_from_ipa(ipa) == png(120)


def test_only_appicon_pngs_count(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/AppIcon60x60@2x.png": png(120),
        "Payload/App.app/LaunchImage.png": png(2048),
        "Payload/App.app/AppIcon.jpg": png(2048),
    })

    assert store.icon_from_ipa(ipa) == png(120)


def test_none_when_the_bundle_carries_no_icon_files(tmp_path, monkeypatch):
    """tvOS bundles keep a layered icon inside Assets.car only, which the
    helper does not flatten: it runs, finds no flat icon, and that is a miss."""
    ipa = make_ipa(tmp_path / "tv.ipa", {
        "Payload/VortX.app/Assets.car": b"BOMStore",
        "Payload/VortX.app/Info.plist": b"<plist/>",
    })
    fake_helper(tmp_path, monkeypatch, succeed_for=None)

    assert store.icon_from_ipa(ipa) is None


def test_none_for_a_non_zip(tmp_path):
    bogus = tmp_path / "not.ipa"
    bogus.write_bytes(b"this is not a zip archive")

    assert store.icon_from_ipa(bogus) is None


def test_none_for_a_missing_file(tmp_path):
    assert store.icon_from_ipa(tmp_path / "gone.ipa") is None


def test_a_file_named_appicon_that_is_not_a_png_does_not_count(tmp_path):
    ipa = make_ipa(tmp_path / "a.ipa", {"Payload/App.app/AppIcon.png": b"not a png at all"})

    assert store.icon_from_ipa(ipa) is None


# ── icon_from_ipa: Assets.car through the helper ───────────────────────────

def test_catalog_icon_is_asked_for_by_the_plists_icon_name(tmp_path, monkeypatch):
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(
        primary={"CFBundleIconName": YOUTUBE_ICON, "CFBundleIconFiles": [YOUTUBE_ICON + "60x60"]},
    ))
    log = fake_helper(tmp_path, monkeypatch)

    assert store.icon_from_ipa(ipa) == png(1024)
    assert helper_calls(log) == [YOUTUBE_ICON]


def test_catalog_names_are_tried_in_plist_order_with_sizes_stripped(tmp_path, monkeypatch):
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(
        primary={"CFBundleIconName": YOUTUBE_ICON,
                 "CFBundleIconFiles": [YOUTUBE_ICON + "60x60", YOUTUBE_ICON + "40x40", "Legacy20x20"]},
        ipad={"CFBundleIconName": "PadIcon", "CFBundleIconFiles": ["PadIcon76x76", "PadIcon83.5x83.5"]},
    ))
    log = fake_helper(tmp_path, monkeypatch, succeed_for="Legacy")

    assert store.icon_from_ipa(ipa) == png(1024)
    assert helper_calls(log) == [YOUTUBE_ICON, "PadIcon", "Legacy"]


def test_none_when_the_helper_finds_no_icon_under_any_name(tmp_path, monkeypatch):
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(
        primary={"CFBundleIconName": YOUTUBE_ICON, "CFBundleIconFiles": [YOUTUBE_ICON + "60x60"]},
    ))
    log = fake_helper(tmp_path, monkeypatch, succeed_for=None)

    assert store.icon_from_ipa(ipa) is None
    assert helper_calls(log) == [YOUTUBE_ICON, "AppIcon"]


def test_appicon_is_the_only_guess_without_a_readable_plist(tmp_path, monkeypatch):
    ipa = catalog_ipa(tmp_path / "yt.ipa", b"<plist/>")
    log = fake_helper(tmp_path, monkeypatch)

    assert store.icon_from_ipa(ipa) == png(1024)
    assert helper_calls(log) == ["AppIcon"]


def test_no_helper_is_unavailable_rather_than_a_miss(tmp_path, monkeypatch):
    """Without the helper the catalog cannot be tried; that is not "no icon"."""
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(primary={"CFBundleIconName": YOUTUBE_ICON}))
    monkeypatch.setattr(store, "icon_helper_path", lambda: None)

    with pytest.raises(store.IconUnavailable):
        store.icon_from_ipa(ipa)


def test_none_without_an_asset_catalog(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/Info.plist": info_plist(primary={"CFBundleIconName": "AppIcon"}),
    })
    log = fake_helper(tmp_path, monkeypatch)

    assert store.icon_from_ipa(ipa) is None
    assert helper_calls(log) == []


def test_a_loose_png_wins_without_asking_the_helper(tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "a.ipa", {
        "Payload/App.app/AppIcon60x60@2x.png": png(120),
        "Payload/App.app/Assets.car": b"BOMStore",
        "Payload/App.app/Info.plist": info_plist(primary={"CFBundleIconName": "AppIcon"}),
    })
    log = fake_helper(tmp_path, monkeypatch)

    assert store.icon_from_ipa(ipa) == png(120)
    assert helper_calls(log) == []


def test_the_extracted_catalog_is_cleaned_up(tmp_path, monkeypatch):
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(primary={"CFBundleIconName": YOUTUBE_ICON}))
    fake_helper(tmp_path, monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # TMPDIR is read once per process, so route mkdtemp here explicitly and
    # prove it was used before asserting the directory is empty again.
    made = []
    real_mkdtemp = store.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        path = real_mkdtemp(dir=scratch, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(store.tempfile, "mkdtemp", mkdtemp)

    assert store.icon_from_ipa(ipa) == png(1024)
    assert made, "the catalog is extracted to a scratch directory"
    assert list(scratch.iterdir()) == []


# ── Environment failures must not be remembered as "no icon" ──────────────────

def _icon_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "ICON_CACHE_DIR", tmp_path / "icons")
    return tmp_path / "icons"


def test_a_missing_helper_leaves_no_miss_marker(tmp_path, monkeypatch):
    """No helper is a property of this Mac, not of the IPA: once catapult-icon
    shows up the icon must still get extracted."""
    icons = _icon_cache(tmp_path, monkeypatch)
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(primary={"CFBundleIconName": YOUTUBE_ICON}))

    assert store.cached_icon(ipa, "a" * 64) is None
    assert not (icons / ("a" * 64 + ".none")).exists()

    fake_helper(tmp_path, monkeypatch)

    assert store.cached_icon(ipa, "a" * 64) == icons / ("a" * 64 + ".png")


def test_a_helper_that_times_out_leaves_no_miss_marker(tmp_path, monkeypatch):
    icons = _icon_cache(tmp_path, monkeypatch)
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(primary={"CFBundleIconName": YOUTUBE_ICON}))
    slow = tmp_path / "slow-helper"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(slow))
    monkeypatch.setattr(store, "HELPER_TIMEOUT", 0.2)

    assert store.cached_icon(ipa, "b" * 64) is None
    assert not (icons / ("b" * 64 + ".none")).exists()


def test_only_a_helper_that_ran_and_found_nothing_writes_the_miss_marker(tmp_path, monkeypatch):
    """Exit 3 means CoreUI could not read the file on this Mac; exit 1 means the
    catalog really has no such icon, which is worth remembering."""
    icons = _icon_cache(tmp_path, monkeypatch)
    ipa = catalog_ipa(tmp_path / "yt.ipa", info_plist(primary={"CFBundleIconName": YOUTUBE_ICON}))
    broken = tmp_path / "broken-helper"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(broken))

    assert store.cached_icon(ipa, "c" * 64) is None
    assert not (icons / ("c" * 64 + ".none")).exists()

    fake_helper(tmp_path, monkeypatch, succeed_for=None)

    assert store.cached_icon(ipa, "c" * 64) is None
    assert (icons / ("c" * 64 + ".none")).exists()


# ── icon_helper_path ───────────────────────────────────────────────────────

def test_helper_lookup_prefers_the_environment(tmp_path, monkeypatch):
    override = tmp_path / "override"
    override.write_text("#!/bin/sh\n")
    override.chmod(0o755)
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(override))

    assert store.icon_helper_path() == override


def test_helper_lookup_skips_an_environment_path_that_cannot_run(tmp_path, monkeypatch):
    not_executable = tmp_path / "override"
    not_executable.write_text("")
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(not_executable))
    built = tmp_path / "catapult-icon"
    built.write_text("#!/bin/sh\n")
    built.chmod(0o755)
    monkeypatch.setattr(store, "ICON_HELPER_PATHS", (tmp_path / "missing", built))

    assert store.icon_helper_path() == built


def test_helper_lookup_is_none_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("CATAPULT_ICON_HELPER", str(tmp_path / "gone"))

    assert store.icon_helper_path() is None


# ── cached_icon ────────────────────────────────────────────────────────────

def _counting_extractor(monkeypatch):
    calls = []
    real = store.icon_from_ipa

    def counted(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(store, "icon_from_ipa", counted)
    return calls


def test_cached_icon_writes_the_png_once_and_reuses_it(icon_dir, tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "a.ipa", {"Payload/App.app/AppIcon60x60@2x.png": png(120)})
    calls = _counting_extractor(monkeypatch)
    sha = "a" * 64

    first = store.cached_icon(ipa, sha)
    second = store.cached_icon(ipa, sha)

    assert first == second == icon_dir / f"{sha}.png"
    assert first.read_bytes() == png(120)
    assert len(calls) == 1


def test_cached_icon_remembers_a_miss_and_does_not_rescan(icon_dir, tmp_path, monkeypatch):
    ipa = make_ipa(tmp_path / "tv.ipa", {"Payload/VortX.app/Assets.car": b"BOMStore"})
    fake_helper(tmp_path, monkeypatch, succeed_for=None)
    calls = _counting_extractor(monkeypatch)
    sha = "b" * 64

    assert store.cached_icon(ipa, sha) is None
    assert store.cached_icon(ipa, sha) is None
    assert (icon_dir / f"{sha}.none").exists()
    assert not (icon_dir / f"{sha}.png").exists()
    assert len(calls) == 1


def test_a_failed_write_leaves_no_half_written_icon(icon_dir, tmp_path, monkeypatch):
    """A concurrent catalog load must never be served a truncated PNG."""
    ipa = make_ipa(tmp_path / "a.ipa", {"Payload/App.app/AppIcon60x60@2x.png": png(120, padding=4096)})
    real = Path.write_bytes

    def torn(self, data):
        real(self, data[:8])
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", torn)

    with pytest.raises(OSError):
        store.cached_icon(ipa, "a" * 64)

    assert list(icon_dir.iterdir()) == []


@pytest.mark.parametrize("sha", ["../x", "a" * 64 + "\n"])
def test_cached_icon_refuses_a_malformed_digest(icon_dir, tmp_path, monkeypatch, sha):
    ipa = make_ipa(tmp_path / "a.ipa", {"Payload/App.app/AppIcon60x60@2x.png": png(120)})
    calls = _counting_extractor(monkeypatch)

    assert store.cached_icon(ipa, sha) is None
    assert calls == []
    assert not icon_dir.exists()


# ── cached_icon_path ───────────────────────────────────────────────────────

def test_cached_icon_path_finds_a_cached_png(icon_dir):
    icon_dir.mkdir()
    sha = "c" * 64
    (icon_dir / f"{sha}.png").write_bytes(png(120))

    assert store.cached_icon_path(sha) == icon_dir / f"{sha}.png"
    assert store.cached_icon_path(sha.upper()) == icon_dir / f"{sha}.png"


@pytest.mark.parametrize("sha", ["", "../x", "c" * 63, "c" * 64 + ".png", "zz" * 32, "c" * 64 + "\n"])
def test_cached_icon_path_rejects_anything_but_a_digest(icon_dir, sha):
    icon_dir.mkdir()
    (icon_dir / ("c" * 64 + ".png")).write_bytes(png(120))

    assert store.cached_icon_path(sha) is None


def test_cached_icon_path_is_none_for_an_unknown_digest(icon_dir):
    assert store.cached_icon_path("d" * 64) is None


# ── owner_avatar_url ───────────────────────────────────────────────────────

def test_github_sources_get_the_owner_avatar():
    source = store.normalize_source("https://github.com/VortXTV/VortX")

    assert store.owner_avatar_url(source) == "https://github.com/VortXTV.png?size=128"


def test_altstore_sources_have_no_avatar():
    source = store.normalize_source("https://example.com/apps.json")

    assert store.owner_avatar_url(source) == ""


# ── download_cache_path ────────────────────────────────────────────────────

def test_download_cache_path_is_keyed_by_the_url(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DOWNLOAD_CACHE_DIR", tmp_path / "downloads")

    path = store.download_cache_path("https://x/a.ipa")

    assert path.parent == tmp_path / "downloads"
    assert path.name == "0ee1f743e6de99cf.ipa"  # first 16 hex of sha256 of the URL
    assert path != store.download_cache_path("https://x/b.ipa")
