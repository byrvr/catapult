"""Provisioning profile parsing.

The profile is a CMS signed blob with an XML plist payload embedded in it.
Catapult needs two things out of it: the entitlements (for signing) and the
real ExpirationDate (so refresh scheduling stops guessing last_installed + 7d).
"""

import datetime as dt
import plistlib

import pytest

from catapult import provisioning


def _profile_blob(payload: dict) -> bytes:
    """Wrap a plist in the CMS-ish envelope a real .mobileprovision has."""
    plist = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    return b"\x30\x82\x0a\x0b" + b"CMS-HEADER-NOISE" + plist + b"\x00\x01TRAILER"


def test_parses_plist_out_of_cms_envelope():
    created = dt.datetime(2026, 8, 1, 12, 0, 0)
    expires = dt.datetime(2026, 8, 8, 12, 0, 0)
    blob = _profile_blob({
        "Name": "Catapult Profile",
        "CreationDate": created,
        "ExpirationDate": expires,
        "Entitlements": {"application-identifier": "TEAM.com.example.app"},
    })

    parsed = provisioning.parse_profile_plist(blob)

    assert parsed["Name"] == "Catapult Profile"
    assert parsed["Entitlements"]["application-identifier"] == "TEAM.com.example.app"


def test_expiration_ts_returns_utc_timestamp():
    expires = dt.datetime(2026, 8, 8, 12, 0, 0)
    blob = _profile_blob({"ExpirationDate": expires})

    ts = provisioning.profile_expiration_ts(blob)

    assert ts == expires.replace(tzinfo=dt.timezone.utc).timestamp()


def test_expiration_ts_is_none_when_absent():
    blob = _profile_blob({"Name": "no dates here"})

    assert provisioning.profile_expiration_ts(blob) is None


def test_expiration_ts_is_none_for_unparseable_blob():
    """A garbage profile must not take down the refresh loop."""
    assert provisioning.profile_expiration_ts(b"not a profile at all") is None


def test_parse_raises_on_unparseable_blob():
    """Signing still needs a hard failure — only the expiry lookup is lenient."""
    with pytest.raises(ValueError):
        provisioning.parse_profile_plist(b"not a profile at all")
