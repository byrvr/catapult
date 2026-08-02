"""Store catalog: asset matching and version ordering.

Grounded in a real source, https://github.com/VortXTV/VortX/releases, which
publishes four IPAs plus unrelated artefacts per release and tags every build
as a pre-release while setting GitHub's prerelease flag to false.
"""

import pytest

from catapult import store


VORTX_ASSETS = [
    "SHA256SUMS-ci.txt",
    "VortX-Android-v0.3.14-beta.12-preview.apk",
    "VortX-iOS-v0.3.14-beta.12-ci.ipa",
    "VortX-macOS-v0.3.14-beta.12-ci.dmg",
    "VortX-tvOS-lite-v0.3.14-beta.12-ci.ipa",
    "VortX-tvOS-v0.3.14-beta.12-ci.ipa",
]


# ── platform classification ────────────────────────────────────────────────

def test_classifies_ios_asset():
    assert store.platform_for_asset("VortX-iOS-v0.3.14-beta.12-ci.ipa") == "ios"


def test_classifies_tvos_asset():
    assert store.platform_for_asset("VortX-tvOS-v0.3.14-beta.12-ci.ipa") == "tvos"


def test_tvos_wins_over_a_substring_match():
    """'tvOS' contains no 'ios', but be explicit that ordering is deliberate."""
    assert store.platform_for_asset("App-tvOS-ios-helper.ipa") == "tvos"


@pytest.mark.parametrize("name", ["App-iPhone.ipa", "App-iPad.ipa", "App-universal.ipa"])
def test_other_ios_tokens(name):
    assert store.platform_for_asset(name) == "ios"


def test_unknown_when_no_token():
    assert store.platform_for_asset("VortX-v1.2.3.ipa") == "unknown"


# ── variant ────────────────────────────────────────────────────────────────

def test_extracts_a_build_variant():
    assert store.variant_for_asset("VortX-tvOS-lite-v0.3.14-beta.12-ci.ipa") == "lite"


def test_no_variant_on_the_plain_build():
    assert store.variant_for_asset("VortX-tvOS-v0.3.14-beta.12-ci.ipa") == ""


def test_no_variant_on_the_ios_build():
    assert store.variant_for_asset("VortX-iOS-v0.3.14-beta.12-ci.ipa") == ""


# ── picking assets out of a release ────────────────────────────────────────

def test_selects_only_ipas():
    picked = store.select_ipa_assets(VORTX_ASSETS)

    assert set(picked) == {
        "VortX-iOS-v0.3.14-beta.12-ci.ipa",
        "VortX-tvOS-lite-v0.3.14-beta.12-ci.ipa",
        "VortX-tvOS-v0.3.14-beta.12-ci.ipa",
    }


def test_ignores_android_and_macos_artifacts():
    picked = store.select_ipa_assets(VORTX_ASSETS)

    assert not any(".apk" in p or ".dmg" in p for p in picked)


def test_finds_the_checksums_asset():
    assert store.find_checksums_asset(VORTX_ASSETS) == "SHA256SUMS-ci.txt"


def test_no_checksums_asset_is_fine():
    assert store.find_checksums_asset(["App.ipa"]) is None


# ── version ordering ───────────────────────────────────────────────────────

def test_beta_12_is_newer_than_beta_9():
    """Lexical comparison gets this backwards, which is the whole point."""
    assert store.version_key("v0.3.14-beta.12") > store.version_key("v0.3.14-beta.9")


def test_release_outranks_its_own_prerelease():
    assert store.version_key("v0.3.14") > store.version_key("v0.3.14-beta.12")


def test_patch_bump_beats_a_prerelease_of_the_same_line():
    assert store.version_key("0.3.15") > store.version_key("0.3.14-beta.12")


def test_leading_v_is_optional():
    assert store.version_key("v1.2.3") == store.version_key("1.2.3")


def test_major_ordering():
    assert store.version_key("2.0.0") > store.version_key("1.99.99")


def test_extra_numeric_segment_is_newer_not_a_prerelease():
    assert store.version_key("1.2.3.4") > store.version_key("1.2.3")


def test_is_newer_helper():
    assert store.is_newer("v0.3.14-beta.12", installed="v0.3.14-beta.9")
    assert not store.is_newer("v0.3.14-beta.9", installed="v0.3.14-beta.12")
    assert not store.is_newer("v0.3.14-beta.12", installed="v0.3.14-beta.12")


def test_is_newer_when_nothing_is_installed():
    assert store.is_newer("v1.0.0", installed=None)


# ── prerelease detection ───────────────────────────────────────────────────

@pytest.mark.parametrize("tag", [
    "v0.3.14-beta.12", "1.0.0-rc1", "2.0-alpha", "v3-nightly", "v1.0-dev", "v9-preview",
])
def test_detects_prerelease_tags(tag):
    assert store.is_prerelease_tag(tag)


@pytest.mark.parametrize("tag", ["v1.2.3", "0.3.14", "v2026.08.01"])
def test_stable_tags_are_not_prereleases(tag):
    assert not store.is_prerelease_tag(tag)


def test_github_prerelease_flag_is_not_trusted():
    """VortX ships v0.3.14-beta.12 with prerelease=false."""
    assert store.is_prerelease_tag("v0.3.14-beta.12")


# ── device fit ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("device_class,platform,expected", [
    ("ipados", "ios", True),
    ("ios", "ios", True),
    ("tvos", "tvos", True),
    ("tvos", "ios", False),
    ("ipados", "tvos", False),
    ("ipados", "unknown", True),   # unresolved: offer it, resolve after download
])
def test_platform_fits_device(device_class, platform, expected):
    assert store.platform_fits_device(platform, device_class) is expected


def test_device_family_resolves_platform():
    assert store.platform_for_device_family([1, 2]) == "ios"
    assert store.platform_for_device_family([3]) == "tvos"
    assert store.platform_for_device_family([]) == "unknown"


# ── source normalisation and adapters ──────────────────────────────────────

def test_normalizes_a_github_url():
    source = store.normalize_source("https://github.com/VortXTV/VortX")

    assert source.kind == "github"
    assert source.id == "github:VortXTV/VortX"


def test_normalizes_a_bare_owner_repo():
    assert store.normalize_source("VortXTV/VortX").id == "github:VortXTV/VortX"


def test_normalizes_a_github_url_with_trailing_slash_and_git():
    assert store.normalize_source("https://github.com/VortXTV/VortX.git/").id == "github:VortXTV/VortX"


def test_treats_other_urls_as_altstore_sources():
    source = store.normalize_source("https://example.com/apps.json")

    assert source.kind == "altstore"


def test_rejects_empty_input():
    with pytest.raises(ValueError):
        store.normalize_source("   ")


def test_prereleases_are_included_by_default():
    """Projects that ship IPAs from CI usually tag every build as a beta;
    VortX has no non-beta tag at all, so defaulting this off shows nothing."""
    assert store.normalize_source("VortXTV/VortX").include_prerelease


def _release(tag, assets, published="2026-08-01T00:00:00Z"):
    return {
        "tag_name": tag,
        "prerelease": False,
        "published_at": published,
        "body": "notes",
        "assets": [{"name": n, "browser_download_url": f"https://x/{n}", "size": 1} for n in assets],
    }


def test_github_catalog_splits_platforms_and_variants():
    source = store.normalize_source("VortXTV/VortX")

    apps = store.catalog_from_github_releases(source, [_release("v0.3.14-beta.12", VORTX_ASSETS)])

    assert {(a.platform, a.variant) for a in apps} == {("ios", ""), ("tvos", ""), ("tvos", "lite")}


def test_github_catalog_keeps_only_the_newest_build_per_variant():
    source = store.normalize_source("VortXTV/VortX")
    releases = [
        _release("v0.3.14-beta.9", ["App-iOS-v0.3.14-beta.9.ipa"]),
        _release("v0.3.14-beta.12", ["App-iOS-v0.3.14-beta.12.ipa"]),
    ]

    apps = store.catalog_from_github_releases(source, releases)

    assert len(apps) == 1
    assert apps[0].version == "v0.3.14-beta.12"


def test_github_catalog_can_exclude_prereleases():
    source = store.normalize_source("VortXTV/VortX")
    source.include_prerelease = False

    apps = store.catalog_from_github_releases(source, [_release("v0.3.14-beta.12", VORTX_ASSETS)])

    assert apps == []


def test_altstore_catalog_reads_the_newest_version():
    source = store.normalize_source("https://example.com/apps.json")
    document = {
        "name": "Example Source",
        "apps": [{
            "name": "Demo",
            "bundleIdentifier": "com.example.demo",
            "developerName": "Someone",
            "iconURL": "https://x/icon.png",
            "versions": [
                {"version": "2.0", "downloadURL": "https://x/demo-2.ipa", "size": 10},
                {"version": "1.0", "downloadURL": "https://x/demo-1.ipa", "size": 9},
            ],
        }],
    }

    apps = store.catalog_from_altstore(source, document)

    assert len(apps) == 1
    assert apps[0].version == "2.0"
    assert apps[0].bundle_id == "com.example.demo"


def test_altstore_catalog_supports_the_legacy_flat_shape():
    source = store.normalize_source("https://example.com/apps.json")
    document = {"apps": [{
        "name": "Old", "bundleIdentifier": "com.example.old",
        "version": "1.2", "downloadURL": "https://x/old.ipa",
    }]}

    apps = store.catalog_from_altstore(source, document)

    assert apps[0].version == "1.2"


def test_altstore_catalog_skips_entries_without_a_download():
    source = store.normalize_source("https://example.com/apps.json")

    assert store.catalog_from_altstore(source, {"apps": [{"name": "Broken"}]}) == []


def test_parses_a_checksums_file():
    text = (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  VortX-iOS.ipa\n"
        "d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592 *VortX-tvOS.ipa\n"
    )

    digests = store.parse_checksums(text)

    assert digests["VortX-iOS.ipa"].startswith("e3b0c442")
    assert digests["VortX-tvOS.ipa"].startswith("d7a8fbb3")
