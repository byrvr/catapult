"""Reading the app version out of a vaulted IPA, for install records that
predate the field."""

import plistlib
import zipfile

from catapult import vault


def _ipa(path, version="21.24.3"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/YouTube.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": "com.google.ios.youtube",
            "CFBundleShortVersionString": version,
        }))
        z.writestr("Payload/YouTube.app/YouTube", b"binary")
    return path


def test_reads_the_short_version_string(tmp_path):
    assert vault.ipa_app_version(_ipa(tmp_path / "a.ipa")) == "21.24.3"


def test_missing_or_broken_files_yield_an_empty_version(tmp_path):
    assert vault.ipa_app_version(tmp_path / "absent.ipa") == ""
    (tmp_path / "junk.ipa").write_bytes(b"not a zip")
    assert vault.ipa_app_version(tmp_path / "junk.ipa") == ""


def test_a_non_dict_or_malformed_plist_yields_an_empty_version(tmp_path):
    with zipfile.ZipFile(tmp_path / "list.ipa", "w") as z:
        z.writestr("Payload/A.app/Info.plist", plistlib.dumps(["not", "a", "dict"]))
    with zipfile.ZipFile(tmp_path / "broken.ipa", "w") as z:
        z.writestr("Payload/A.app/Info.plist", b"<?xml version='1.0'?><plist><dict><key>x")

    assert vault.ipa_app_version(tmp_path / "list.ipa") == ""
    assert vault.ipa_app_version(tmp_path / "broken.ipa") == ""
