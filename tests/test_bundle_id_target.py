"""Choosing which bundle ID to sign and install under.

Catapult prefers to update an existing sideloaded copy in place. But the
device lookup also finds the App Store build, and adopting *its* bundle ID
means signing a development build as e.g. com.google.ios.youtube and asking
installd to replace an App Store app. installd refuses:

    IXErrorDomain Code=46 "A coordinated app install already exists for
    [com.google.ios.youtube/...] with scope IXCoordinatorScopeGlobal
    (creator App Store)"
"""


def choose_target_bundle_id(
    *, original_bundle_id: str, legacy_bundle_id: str,
    installed_bundle_id: str | None, recorded_bundle_id: str | None,
) -> str:
    """Mirror of the selection in server.py's install flow."""
    if installed_bundle_id == original_bundle_id and not recorded_bundle_id:
        installed_bundle_id = None
    return installed_bundle_id or recorded_bundle_id or legacy_bundle_id


ORIGINAL = "com.google.ios.youtube"
LEGACY = "com.catapult.TEAM123.com-google-ios-youtube"


def test_does_not_try_to_replace_an_app_store_build():
    target = choose_target_bundle_id(
        original_bundle_id=ORIGINAL,
        legacy_bundle_id=LEGACY,
        installed_bundle_id=ORIGINAL,   # the App Store copy
        recorded_bundle_id=None,        # we never installed it
    )

    assert target == LEGACY


def test_updates_our_own_sideloaded_copy_in_place():
    target = choose_target_bundle_id(
        original_bundle_id=ORIGINAL,
        legacy_bundle_id=LEGACY,
        installed_bundle_id=LEGACY,
        recorded_bundle_id=None,
    )

    assert target == LEGACY


def test_keeps_the_original_id_when_we_installed_it_that_way():
    """Some apps are not on the App Store, so a prior Catapult install may
    legitimately own the real bundle ID."""
    target = choose_target_bundle_id(
        original_bundle_id=ORIGINAL,
        legacy_bundle_id=LEGACY,
        installed_bundle_id=ORIGINAL,
        recorded_bundle_id=ORIGINAL,
    )

    assert target == ORIGINAL


def test_falls_back_to_a_recorded_id_when_nothing_is_installed():
    target = choose_target_bundle_id(
        original_bundle_id=ORIGINAL,
        legacy_bundle_id=LEGACY,
        installed_bundle_id=None,
        recorded_bundle_id=LEGACY,
    )

    assert target == LEGACY


def test_namespaces_a_clean_first_install():
    target = choose_target_bundle_id(
        original_bundle_id=ORIGINAL,
        legacy_bundle_id=LEGACY,
        installed_bundle_id=None,
        recorded_bundle_id=None,
    )

    assert target == LEGACY
