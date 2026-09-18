"""Cleanup only ever offers things that are provably dead."""

import json
import pathlib

import pytest

from catapult import cleanup, vault


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    d = tmp_path / "uploads"
    d.mkdir()
    monkeypatch.setattr(vault, "VAULT_DIR", tmp_path / "vault", raising=False)
    return d


def ipa(directory, name, size=10):
    p = directory / name
    p.write_bytes(b"x" * size)
    return p


# ── orphaned files ──

def test_a_file_a_record_points_at_is_not_orphaned(uploads):
    kept = ipa(uploads, "kept.ipa")
    orphan = ipa(uploads, "orphan.ipa")
    installs = [{"ipa_path": str(kept)}]
    assert cleanup.orphaned_ipas(uploads, installs) == [orphan]


def test_a_record_with_a_dead_path_protects_nothing(uploads):
    orphan = ipa(uploads, "orphan.ipa")
    installs = [{"ipa_path": "/var/folders/gone/catapult_uploads/missing.ipa"}]
    assert cleanup.orphaned_ipas(uploads, installs) == [orphan]


def test_non_ipa_files_are_left_alone(uploads):
    ipa(uploads, "notes.txt")
    orphan = ipa(uploads, "orphan.ipa")
    assert cleanup.orphaned_ipas(uploads, []) == [orphan]


def test_a_missing_upload_directory_is_not_an_error(tmp_path):
    assert cleanup.orphaned_ipas(tmp_path / "nope", []) == []


def test_every_file_is_orphaned_when_there_are_no_records(uploads):
    a, b = ipa(uploads, "a.ipa"), ipa(uploads, "b.ipa")
    assert cleanup.orphaned_ipas(uploads, []) == [a, b]


# ── dead records ──

def test_a_record_whose_ipa_is_gone_is_unresolvable(uploads):
    kept = ipa(uploads, "kept.ipa")
    installs = [{"ipa_path": str(kept)}, {"ipa_path": "/gone/x.ipa"}]
    assert cleanup.unresolvable_records(installs) == [{"ipa_path": "/gone/x.ipa"}]


# ── App IDs ──

def app(**kw):
    base = {
        "app_id_id": "ABC123",
        "is_extension": False,
        "can_reinstall": False,
        "saved_ipa_exists": False,
        "days_left": None,
        "is_catapult": True,
    }
    base.update(kw)
    return base


def test_an_app_id_with_nothing_behind_it_is_deletable():
    assert cleanup.deletable_app_ids([app()]) == [app()]


def test_a_reinstallable_app_is_never_offered():
    assert cleanup.deletable_app_ids([app(can_reinstall=True, saved_ipa_exists=True)]) == []


def test_an_app_with_a_saved_ipa_is_never_offered():
    assert cleanup.deletable_app_ids([app(saved_ipa_exists=True)]) == []


def test_a_live_install_is_never_offered_even_without_a_saved_ipa():
    # Deleting this App ID would break an app currently working on a device.
    assert cleanup.deletable_app_ids([app(days_left=5)]) == []


def test_an_expired_install_is_offered():
    assert len(cleanup.deletable_app_ids([app(days_left=0)])) == 1
    assert len(cleanup.deletable_app_ids([app(days_left=-3)])) == 1


def test_an_extension_is_never_offered():
    assert cleanup.deletable_app_ids([app(is_extension=True)]) == []


def test_a_row_with_no_app_id_is_never_offered():
    # History-only rows have no slot on the account to delete.
    assert cleanup.deletable_app_ids([app(app_id_id="")]) == []


# ── deleting ──

def test_delete_reports_what_it_freed(uploads):
    a = ipa(uploads, "a.ipa", size=100)
    b = ipa(uploads, "b.ipa", size=50)
    deleted, freed, errors = cleanup.delete_files([a, b])
    assert (deleted, freed, errors) == (2, 150, [])
    assert not a.exists() and not b.exists()


def test_delete_keeps_going_past_a_failure(uploads):
    a = ipa(uploads, "a.ipa", size=7)
    deleted, freed, errors = cleanup.delete_files([uploads / "ghost.ipa", a])
    assert deleted == 1 and freed == 7
    assert len(errors) == 1 and "ghost.ipa" in errors[0]


def test_total_bytes_ignores_files_that_vanished(uploads):
    a = ipa(uploads, "a.ipa", size=42)
    assert cleanup.total_bytes([a, uploads / "ghost.ipa"]) == 42


def test_an_app_id_catapult_did_not_create_is_never_offered():
    # The account also holds real App Store identifiers. One was offered here
    # and only Apple's own refusal ("in use by the App Store") stopped it.
    assert cleanup.deletable_app_ids([app(is_catapult=False)]) == []


def test_only_catapult_slots_survive_a_mixed_list():
    apps = [
        app(app_id_id="MINE", is_catapult=True),
        app(app_id_id="APPSTORE", is_catapult=False),
    ]
    assert [a["app_id_id"] for a in cleanup.deletable_app_ids(apps)] == ["MINE"]
