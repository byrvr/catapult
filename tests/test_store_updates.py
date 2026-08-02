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
