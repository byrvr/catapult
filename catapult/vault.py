"""Durable local IPA vault.

Uploads should not live in temporary paths. Auto-refresh and cross-device sync
need a stable content-addressed copy that survives app restarts and can be
matched to a remote encrypted blob by SHA-256.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


STATE_DIR = Path.home() / ".catapult"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Catapult"
IPA_VAULT_DIR = APP_SUPPORT_DIR / "IPAs"

CHUNK_SIZE = 1024 * 1024


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vault_path(sha256: str) -> Path:
    if not sha256 or any(char not in "0123456789abcdef" for char in sha256.lower()):
        raise ValueError("Invalid IPA SHA-256")
    return IPA_VAULT_DIR / f"{sha256.lower()}.ipa"


def store_ipa(path: str | Path, *, original_filename: str = "") -> dict:
    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"IPA file is missing: {source}")

    digest = sha256_file(source)
    dest = vault_path(digest)
    IPA_VAULT_DIR.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        tmp = dest.with_suffix(".ipa.tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(dest)

    return {
        "sha256": digest,
        "path": str(dest),
        "size": dest.stat().st_size,
        "original_filename": original_filename or source.name,
    }


def resolve_ipa_path(record: dict) -> Path | None:
    ipa_path = record.get("ipa_path", "")
    if ipa_path:
        candidate = Path(ipa_path).expanduser()
        if candidate.exists():
            return candidate

    digest = record.get("ipa_sha256", "")
    if digest:
        candidate = vault_path(digest)
        if candidate.exists():
            return candidate
    return None


def has_ipa(sha256: str | None) -> bool:
    if not sha256:
        return False
    try:
        return vault_path(sha256).exists()
    except ValueError:
        return False


def vault_metadata(sha256: str | None) -> dict | None:
    if not sha256:
        return None
    try:
        path = vault_path(sha256)
    except ValueError:
        return None
    if not path.exists():
        return None
    return {
        "sha256": sha256,
        "path": str(path),
        "size": path.stat().st_size,
    }

