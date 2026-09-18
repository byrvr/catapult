"""Finding what is safe to delete: IPAs nothing points at, and records that can
never resolve again.

Install records predate the vault and point at directories the app no longer
uses — an old Application Support folder and the system temp dir, which macOS
purges on its own. Those records can never resolve, their apps show as
un-reinstallable, and meanwhile the files that replaced them pile up in the
upload directory with nothing referencing them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from catapult import vault

logger = logging.getLogger(__name__)

IPA_SUFFIX = ".ipa"


def referenced_paths(installs: list[dict] | None) -> set[Path]:
    """Every file some install record can still resolve to."""
    found: set[Path] = set()
    for record in installs or []:
        resolved = vault.resolve_ipa_path(record)
        if resolved:
            try:
                found.add(resolved.resolve())
            except OSError:
                found.add(resolved)
    return found


def orphaned_ipas(upload_dir: str | Path, installs: list[dict] | None) -> list[Path]:
    """IPAs in the upload directory that no install record points at."""
    directory = Path(upload_dir)
    if not directory.is_dir():
        return []
    referenced = referenced_paths(installs)
    orphans = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix != IPA_SUFFIX:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved not in referenced:
            orphans.append(path)
    return sorted(orphans)


def unresolvable_records(installs: list[dict] | None) -> list[dict]:
    """Records whose IPA is gone for good, by path and by digest."""
    return [r for r in installs or [] if vault.resolve_ipa_path(r) is None]


def deletable_app_ids(apps: list[dict] | None) -> list[dict]:
    """App IDs with nothing behind them and nothing live in front of them.

    Deliberately conservative, and the first rule matters most: an App ID this
    app did not create is never offered. The account also holds real App Store
    identifiers, and one of those was offered here before Apple refused the
    delete with "appears to be in use by the App Store". That refusal was luck,
    not design.

    An extension is never offered either: it belongs to a parent app, and
    removing it alone breaks that app. Neither is an App ID whose install has
    not expired yet — deleting it would kill a working app on a device. All of
    them remain available one by one.
    """
    deletable = []
    for app in apps or []:
        if not app.get("app_id_id") or app.get("is_extension"):
            continue
        if not app.get("is_catapult"):
            continue
        if app.get("can_reinstall") or app.get("saved_ipa_exists"):
            continue
        days_left = app.get("days_left")
        if isinstance(days_left, (int, float)) and days_left > 0:
            continue
        deletable.append(app)
    return deletable


def total_bytes(paths) -> int:
    total = 0
    for path in paths:
        try:
            total += Path(path).stat().st_size
        except OSError:
            continue
    return total


def delete_files(paths) -> tuple[int, int, list[str]]:
    """Delete the given files. Returns (deleted, bytes freed, errors)."""
    deleted = 0
    freed = 0
    errors: list[str] = []
    for path in paths:
        target = Path(path)
        try:
            size = target.stat().st_size
            target.unlink()
        except OSError as e:
            errors.append(f"{target.name}: {e}")
            continue
        deleted += 1
        freed += size
    return deleted, freed, errors
