"""Signing identifier for nested bundles.

Repackaged IPAs frequently ship frameworks whose signing identifier disagrees
with their CFBundleIdentifier. ellekit is the common one: it ships as
CydiaSubstrate.framework, its Info.plist says "ellekit", and the original
signature says "CydiaSubstrate". Preserving the original identifier while
re-signing makes installd reject the whole install:

    MismatchedBundleIDSigningIdentifier: Code signing identifier
    (CydiaSubstrate) does not match bundle identifier (ellekit)
"""

import plistlib

from catapult.signer import Signer


def _framework(tmp_path, name: str, bundle_id: str | None, *, nested: bool = False):
    path = tmp_path / name
    path.mkdir(parents=True)
    if bundle_id is None:
        return path
    plist_dir = path / "Resources" if nested else path
    plist_dir.mkdir(parents=True, exist_ok=True)
    (plist_dir / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id})
    )
    return path


def test_reads_the_identifier_from_a_framework(tmp_path):
    path = _framework(tmp_path, "CydiaSubstrate.framework", "ellekit")

    assert Signer._nested_bundle_identifier(path) == "ellekit"


def test_reads_the_identifier_from_resources(tmp_path):
    """macOS-style frameworks keep Info.plist under Resources/."""
    path = _framework(tmp_path, "Other.framework", "com.example.other", nested=True)

    assert Signer._nested_bundle_identifier(path) == "com.example.other"


def test_returns_none_without_a_plist(tmp_path):
    """A bare dylib has no bundle; codesign derives the identifier itself."""
    path = _framework(tmp_path, "libswiftCore.dylib", None)

    assert Signer._nested_bundle_identifier(path) is None


def test_returns_none_for_a_file(tmp_path):
    path = tmp_path / "thing.dylib"
    path.write_bytes(b"not a bundle")

    assert Signer._nested_bundle_identifier(path) is None


def test_returns_none_for_an_empty_identifier(tmp_path):
    path = _framework(tmp_path, "Empty.framework", "")

    assert Signer._nested_bundle_identifier(path) is None


def test_survives_a_corrupt_plist(tmp_path):
    """A malformed plist must not take down the whole signing run."""
    path = tmp_path / "Broken.framework"
    path.mkdir()
    (path / "Info.plist").write_bytes(b"\x00 definitely not a plist")

    assert Signer._nested_bundle_identifier(path) is None
