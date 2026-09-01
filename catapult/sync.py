"""Encrypted cross-device sync for Catapult install records and IPA blobs."""

from __future__ import annotations

import base64
import datetime as _dt
import errno
import hashlib
import hmac
import json
import logging
import os
import secrets
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from catapult import recoverykey, vault
from catapult.refresh import (
    _keychain_get,
    _keychain_set,
    load_state,
    save_state,
)

logger = logging.getLogger(__name__)

SYNC_KEYCHAIN_SERVICE_ACCOUNT_PREFIX = "sync-key"
SYNC_MANIFEST_VERSION = 1

# Legacy configuration. Read for one release so existing users are not
# stranded, then imported into CONFIG_PATH and never written again.
CONFIG_ENV_PATH = Path.home() / ".catapult" / "config.env"

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Catapult"
CONFIG_PATH = APP_SUPPORT_DIR / "sync.json"

# The default vault lives in the user's own iCloud Drive. This is ordinary
# POSIX I/O from a non-sandboxed process: no entitlement, no Team ID, no
# notarization — which matters because an ad-hoc-signed bundle declaring any
# non-allowlisted entitlement is SIGKILLed at launch. Per-user isolation is
# structural: it is the user's own iCloud account.
ICLOUD_DRIVE_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
ICLOUD_VAULT_PATH = ICLOUD_DRIVE_ROOT / "Catapult"


def icloud_drive_available() -> bool:
    """True when iCloud Drive is switched on for this user."""
    return ICLOUD_DRIVE_ROOT.is_dir()


def _parse_config_env() -> dict[str, str]:
    try:
        lines = CONFIG_ENV_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.warning("Could not read sync config %s: %s", CONFIG_ENV_PATH, e)
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not key.startswith("CATAPULT_"):
            continue
        value = value.strip()
        try:
            parsed = shlex.split(value)
            if len(parsed) == 1:
                value = parsed[0]
        except ValueError:
            value = value.strip("\"'")
        values[key] = value
    return values


def _sync_setting(name: str, default: str = "") -> str:
    env_value = os.environ.get(name)
    if env_value:
        return env_value.strip()
    return _parse_config_env().get(name, default).strip()


@dataclass(frozen=True)
class SyncConfig:
    provider: str = "disabled"
    folder: Path | None = None
    r2_endpoint: str = ""
    r2_bucket: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""

    @classmethod
    def from_env(cls) -> "SyncConfig":
        provider = _sync_setting("CATAPULT_SYNC_PROVIDER", "disabled").lower()
        folder = os.path.expandvars(_sync_setting("CATAPULT_SYNC_FOLDER"))
        return cls(
            provider=provider or "disabled",
            folder=Path(folder).expanduser() if folder else None,
            r2_endpoint=_sync_setting("CATAPULT_R2_ENDPOINT").rstrip("/"),
            r2_bucket=_sync_setting("CATAPULT_R2_BUCKET"),
            r2_access_key_id=_sync_setting("CATAPULT_R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_sync_setting("CATAPULT_R2_SECRET_ACCESS_KEY"),
        )

    @classmethod
    def load(cls) -> "SyncConfig":
        """Stored settings, falling back to the legacy env/dotfile once.

        The dotfile was never a good home for this: a Finder-launched app
        inherits no shell environment, which is the root of "sync does not
        really work".
        """
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls.from_env()
        except (OSError, json.JSONDecodeError):
            logger.warning("Sync settings at %s are unreadable", CONFIG_PATH)
            return cls.from_env()

        folder = data.get("folder") or ""
        return cls(
            provider=(data.get("provider") or "disabled").lower(),
            folder=Path(folder).expanduser() if folder else None,
            r2_endpoint=(data.get("r2_endpoint") or "").rstrip("/"),
            r2_bucket=data.get("r2_bucket") or "",
            r2_access_key_id=_keychain_get("sync-r2-access-key-id") or "",
            r2_secret_access_key=_keychain_get("sync-r2-secret-access-key") or "",
        )

    def save(self) -> None:
        """Persist settings. R2 credentials go to the Keychain, not the file."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    "provider": self.provider,
                    "folder": str(self.folder) if self.folder else "",
                    "r2_endpoint": self.r2_endpoint,
                    "r2_bucket": self.r2_bucket,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if self.r2_access_key_id:
            _keychain_set("sync-r2-access-key-id", self.r2_access_key_id)
        if self.r2_secret_access_key:
            _keychain_set("sync-r2-secret-access-key", self.r2_secret_access_key)

    @property
    def configured(self) -> bool:
        if self.provider == "folder":
            return self.folder is not None
        if self.provider == "r2":
            return all([
                self.r2_endpoint,
                self.r2_bucket,
                self.r2_access_key_id,
                self.r2_secret_access_key,
            ])
        return False


def _normalize_key(raw: str) -> bytes:
    value = raw.strip()
    if not value:
        raise ValueError("Sync key is empty")
    for decoder in (
        lambda text: base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)),
        lambda text: bytes.fromhex(text),
    ):
        try:
            decoded = decoder(value)
            if len(decoded) == 32:
                return decoded
        except Exception:
            pass
    return hashlib.sha256(value.encode("utf-8")).digest()


def _sync_key_account(apple_id: str, team_id: str) -> str:
    return f"{SYNC_KEYCHAIN_SERVICE_ACCOUNT_PREFIX}:{apple_id}:{team_id}"


def _recovery_key_account(team_id: str) -> str:
    return f"recovery-key:{team_id}"


def cached_recovery_key(team_id: str) -> bytes | None:
    """The recovery key this Mac has already been given, if any.

    A cache, not a source of truth: its absence means "ask the user for the
    key", never "mint a new one".
    """
    stored = _keychain_get(_recovery_key_account(team_id))
    if not stored:
        return None
    try:
        return recoverykey.decode(stored)
    except ValueError:
        logger.warning("Cached recovery key is unreadable — discarding")
        return None


def cache_recovery_key(team_id: str, key: bytes) -> None:
    if not _keychain_set(_recovery_key_account(team_id), recoverykey.encode(key)):
        logger.warning("Could not cache recovery key in Keychain")


def forget_recovery_key(team_id: str) -> None:
    from catapult.refresh import _keychain_delete

    _keychain_delete(_recovery_key_account(team_id))


def legacy_sync_key() -> bytes | None:
    """The pre-vault CATAPULT_SYNC_KEY, if one is still configured.

    Adopted verbatim as the data key on migration so every already-uploaded
    blob stays readable and nothing is re-encrypted or re-uploaded.
    """
    env_key = _sync_setting("CATAPULT_SYNC_KEY")
    if not env_key:
        return None
    return _normalize_key(env_key)


def _encrypt_json(key: bytes, payload: dict) -> bytes:
    return _encrypt_bytes(key, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _decrypt_json(key: bytes, payload: bytes) -> dict:
    if not payload:
        return {}
    return json.loads(_decrypt_bytes(key, payload).decode("utf-8"))


def _encrypt_bytes(key: bytes, payload: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, payload, None)
    return V1_MAGIC + nonce + ciphertext


def _decrypt_bytes(key: bytes, payload: bytes) -> bytes:
    if not payload.startswith(V1_MAGIC):
        raise ValueError("Unsupported encrypted sync payload")
    body = payload[len(V1_MAGIC):]
    nonce = body[:12]
    ciphertext = body[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None)


# ── Vault envelope ──────────────────────────────────────────────────────────
#
# A random 256-bit data key (DK) encrypts the manifest and every blob. DK is
# wrapped under a KEK derived from the generated 128-bit recovery key (RK), and
# the wrapped form lives in a PLAINTEXT vault.json beside the manifest. So a
# second Mac needs exactly one thing: RK.
#
# No password KDF is involved: RK is uniformly random, so the offline attack
# floor is already 2**128 and stretching would only add a tuning problem across
# the old Intel Macs macOS 14 still supports.
#
# The AEAD tag on the ~48-byte wrapped key IS the wrong-key verifier. A separate
# hash verifier would add no usability and give an attacker a cheaper oracle.

VAULT_FORMAT = 2
VAULT_WRAP_ALG = "hkdf-sha256+aes256gcm"
_WRAP_INFO = b"catapult/recovery/v1"


class WrongRecoveryKey(Exception):
    """The supplied recovery key does not open this vault."""


def new_data_key() -> bytes:
    return secrets.token_bytes(32)


def derive_kek(recovery_key: bytes, team_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=team_id.encode("utf-8"),
        info=_WRAP_INFO,
    ).derive(recovery_key)


def wrap_data_key(data_key: bytes, recovery_key: bytes, team_id: str) -> dict:
    nonce = secrets.token_bytes(12)
    kek = derive_kek(recovery_key, team_id)
    ciphertext = AESGCM(kek).encrypt(nonce, data_key, team_id.encode("utf-8"))
    return {
        "alg": VAULT_WRAP_ALG,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ct": base64.b64encode(ciphertext).decode("ascii"),
    }


def unwrap_data_key(vault_doc: dict, recovery_key: bytes, team_id: str) -> bytes:
    """Recover the data key, or raise WrongRecoveryKey.

    Fails in about a millisecond, so a mistyped key is reported immediately
    rather than after a 500 MB download.
    """
    wrap = vault_doc.get("wrap") or {}
    if wrap.get("alg") != VAULT_WRAP_ALG:
        raise WrongRecoveryKey(f"Unsupported vault wrapping: {wrap.get('alg')!r}")
    try:
        nonce = base64.b64decode(wrap["nonce"])
        ciphertext = base64.b64decode(wrap["ct"])
        kek = derive_kek(recovery_key, team_id)
        return AESGCM(kek).decrypt(nonce, ciphertext, team_id.encode("utf-8"))
    except (InvalidTag, KeyError, ValueError) as e:
        raise WrongRecoveryKey("That recovery key does not open this vault") from e


def new_vault(team_id: str) -> tuple[dict, bytes, bytes]:
    """Create a fresh vault document. Returns (vault_doc, data_key, recovery_key)."""
    data_key = new_data_key()
    recovery_key = recoverykey.generate()
    return (
        {
            "vault_format": VAULT_FORMAT,
            "team_id": team_id,
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "wrap": wrap_data_key(data_key, recovery_key, team_id),
        },
        data_key,
        recovery_key,
    )


# ── Streaming blob format ───────────────────────────────────────────────────
#
# v1 held the whole IPA in memory twice over. v2 streams 4 MiB chunks, each
# sealed with a nonce derived from a per-file nonce plus a counter, and each
# chunk's AAD carries its index and a final-chunk flag so truncation and
# reordering are both detected.

V1_MAGIC = b"catapult-sync-v1\n"
V2_MAGIC = b"catapult-sync-v2\n"
CHUNK_SIZE = 4 * 1024 * 1024
_FILE_NONCE_BYTES = 8
_LENGTH_BYTES = 4


def _chunk_nonce(file_nonce: bytes, index: int) -> bytes:
    return file_nonce + index.to_bytes(4, "big")


def _chunk_aad(index: int, final: bool) -> bytes:
    return index.to_bytes(4, "big") + (b"\x01" if final else b"\x00")


def encrypt_file(key: bytes, src: Path, dst: Path) -> None:
    """Encrypt src to dst in chunks, holding at most one chunk in memory."""
    aes = AESGCM(key)
    file_nonce = secrets.token_bytes(_FILE_NONCE_BYTES)
    with src.open("rb") as fin, dst.open("wb") as fout:
        fout.write(V2_MAGIC)
        fout.write(file_nonce)
        index = 0
        chunk = fin.read(CHUNK_SIZE)
        while True:
            following = fin.read(CHUNK_SIZE)
            final = not following
            sealed = aes.encrypt(
                _chunk_nonce(file_nonce, index), chunk, _chunk_aad(index, final)
            )
            fout.write(len(sealed).to_bytes(_LENGTH_BYTES, "big"))
            fout.write(sealed)
            if final:
                return
            chunk = following
            index += 1


def decrypt_file(key: bytes, src: Path, dst: Path) -> None:
    """Decrypt a v2 blob, or a legacy v1 blob, writing to dst.

    Leaves no output behind on failure — a partially written IPA would be
    installed and fail confusingly.
    """
    with src.open("rb") as fin:
        magic = fin.read(len(V2_MAGIC))
        if magic == V1_MAGIC:
            payload = magic + fin.read()
            dst.write_bytes(_decrypt_bytes(key, payload))
            return
        if magic != V2_MAGIC:
            raise ValueError("Unsupported encrypted sync payload")

        aes = AESGCM(key)
        file_nonce = fin.read(_FILE_NONCE_BYTES)
        if len(file_nonce) != _FILE_NONCE_BYTES:
            raise ValueError("Encrypted blob is truncated")

        tmp = dst.with_suffix(dst.suffix + ".part")
        index = 0
        saw_final = False
        try:
            with tmp.open("wb") as fout:
                while True:
                    header = fin.read(_LENGTH_BYTES)
                    if not header:
                        break
                    if len(header) != _LENGTH_BYTES:
                        raise ValueError("Encrypted blob is truncated")
                    length = int.from_bytes(header, "big")
                    sealed = fin.read(length)
                    if len(sealed) != length:
                        raise ValueError("Encrypted blob is truncated")
                    nonce = _chunk_nonce(file_nonce, index)
                    try:
                        plain = aes.decrypt(nonce, sealed, _chunk_aad(index, False))
                    except InvalidTag:
                        plain = aes.decrypt(nonce, sealed, _chunk_aad(index, True))
                        saw_final = True
                    fout.write(plain)
                    if saw_final:
                        break
                    index += 1
            if not saw_final:
                raise ValueError("Encrypted blob is truncated")
            tmp.replace(dst)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


class RemoteStore:
    async def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    async def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    async def exists(self, key: str) -> bool:
        return await self.get(key) is not None

    async def put_file(self, key: str, path: Path) -> None:
        """Upload from disk. Overridden where streaming is possible."""
        await self.put(key, path.read_bytes())

    async def get_file(self, key: str, dest: Path) -> bool:
        data = await self.get(key)
        if data is None:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True

    async def delete(self, key: str) -> None:
        raise NotImplementedError

    async def archive_team(self, team_id: str) -> str:
        """Move a team's vault out of the way before a replacement is created.

        Returns a short note about what happened to the old data. This default
        removes the descriptor and the manifest, which is enough for the next
        sync to start from an empty manifest and re-upload from the local
        vault; blobs written under the old data key are left in place.
        """
        for key in (_manifest_key(team_id), _vault_key(team_id)):
            await self.delete(key)
        return (
            "The old vault descriptor and manifest were removed; encrypted IPA "
            "blobs from the old vault were left in place."
        )


class FolderStore(RemoteStore):
    """Any folder on disk — an iCloud Drive path, Dropbox, a network share.

    Writes are staged OUTSIDE the synced root and moved in atomically. Staging
    inside it means the sync client sees a partial multi-hundred-megabyte temp
    file and pushes it to every other Mac before it is renamed away.
    """

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        cleaned = key.strip("/")
        return self.root / cleaned

    @contextmanager
    def _staged(self, target: Path):
        """Yield a temp path outside the root, then move it onto target."""
        target.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix="catapult-sync-"))
        staged = staging_dir / target.name
        try:
            yield staged
            try:
                staged.replace(target)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                # A File Provider domain is its own mount point, so the move can
                # cross devices. Fall back to a hidden temp inside the root.
                fallback = target.with_name(f".{target.name}.part")
                shutil.copyfile(staged, fallback)
                fallback.replace(target)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        return path.read_bytes()

    async def put(self, key: str, data: bytes) -> None:
        target = self._path(key)
        with self._staged(target) as staged:
            staged.write_bytes(data)

    async def put_file(self, key: str, path: Path) -> None:
        target = self._path(key)
        with self._staged(target) as staged:
            shutil.copyfile(path, staged)

    async def get_file(self, key: str, dest: Path) -> bool:
        source = self._path(key)
        if not source.exists():
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        return True

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def archive_team(self, team_id: str) -> str:
        team_dir = self._path(f"teams/{team_id}")
        if not team_dir.exists():
            return "There was no previous vault to keep."
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archived = team_dir.with_name(f"{team_dir.name}.replaced-{stamp}")
        team_dir.rename(archived)
        return (
            f"The previous vault was moved to {archived.name} inside the sync "
            "folder. Delete it once you are sure you no longer need it."
        )


class R2Store(RemoteStore):
    def __init__(self, config: SyncConfig):
        self.endpoint = config.r2_endpoint
        self.bucket = config.r2_bucket
        self.access_key = config.r2_access_key_id
        self.secret_key = config.r2_secret_access_key
        self.region = "auto"
        self.service = "s3"

    def _url(self, key: str) -> str:
        return f"{self.endpoint}/{quote(self.bucket)}/{quote(key.strip('/'), safe='/')}"

    def _sign_headers(self, method: str, url: str, payload: bytes = b"") -> dict:
        parsed = urlparse(url)
        host = parsed.netloc
        canonical_uri = parsed.path or "/"
        now = _dt.datetime.now(_dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()
        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join([
            method,
            canonical_uri,
            parsed.query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signing_key = self._signing_key(date_stamp)
        signature = hmac.new(
            signing_key,
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }

    def _signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_key).encode("utf-8")
        date_key = hmac.new(key, date_stamp.encode("utf-8"), hashlib.sha256).digest()
        region_key = hmac.new(date_key, self.region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, self.service.encode("utf-8"), hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()

    async def get(self, key: str) -> bytes | None:
        url = self._url(key)
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url, headers=self._sign_headers("GET", url))
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.content

    async def put(self, key: str, data: bytes) -> None:
        url = self._url(key)
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.put(
                url,
                content=data,
                headers=self._sign_headers("PUT", url, data),
            )
        response.raise_for_status()

    async def exists(self, key: str) -> bool:
        url = self._url(key)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.head(url, headers=self._sign_headers("HEAD", url))
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True

    async def delete(self, key: str) -> None:
        url = self._url(key)
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(url, headers=self._sign_headers("DELETE", url))
        if response.status_code == 404:
            return
        response.raise_for_status()


def _store_from_config(config: SyncConfig) -> RemoteStore | None:
    if not config.configured:
        return None
    if config.provider == "folder" and config.folder:
        return FolderStore(config.folder)
    if config.provider == "r2":
        return R2Store(config)
    return None


def resolve_vault_state(*, vault_doc: dict | None, have_key: bool) -> str:
    """Decide what the vault needs, given what is remote and what is local.

    The old get_sync_key() minted a fresh random key when the Keychain was
    empty. On a second Mac that silently created an incompatible vault and
    started uploading into it, rather than reporting that the existing vault
    was locked. An empty Keychain plus an existing remote vault must mean
    "locked", never "make a new one".
    """
    if vault_doc is None:
        # No remote vault: nothing to unlock, whether or not we hold a key.
        return "needs_setup"
    return "ok" if have_key else "locked"


def _vault_key(team_id: str) -> str:
    return f"teams/{team_id}/vault.json"


def _lease_key(team_id: str) -> str:
    return f"teams/{team_id}/lease.json"


def _manifest_key(team_id: str) -> str:
    return f"teams/{team_id}/manifest.json.enc"


def _ipa_key(team_id: str, sha256: str) -> str:
    return f"teams/{team_id}/ipas/{sha256}.ipa.enc"


def _default_manifest(apple_id: str, team_id: str) -> dict:
    return {
        "version": SYNC_MANIFEST_VERSION,
        "apple_id": apple_id,
        "team_id": team_id,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "installs": [],
    }


def _install_key(record: dict) -> tuple[str, str, str]:
    return (
        record.get("device_udid", ""),
        record.get("bundle_id") or record.get("source_bundle_id", ""),
        record.get("ipa_sha256", ""),
    )


def _merge_installs(local: list[dict], remote: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str, str], dict] = {}
    for record in [*remote, *local]:
        key = _install_key(record)
        existing = merged.get(key)
        if not existing or record.get("last_installed", 0) >= existing.get("last_installed", 0):
            merged[key] = {**existing, **record} if existing else dict(record)
    return sorted(
        merged.values(),
        key=lambda item: (item.get("app_name", ""), item.get("device_name", ""), item.get("last_installed", 0)),
    )


async def load_vault_doc(store: RemoteStore, team_id: str) -> dict | None:
    """Read the plaintext vault descriptor, if the vault has been created."""
    payload = await store.get(_vault_key(team_id))
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Remote vault.json is unreadable")
        return None


async def open_vault(store: RemoteStore, team_id: str) -> tuple[str, bytes | None]:
    """Resolve the vault into (state, data_key).

    Handles the one-time migration off CATAPULT_SYNC_KEY: the legacy key is
    adopted verbatim as the data key and wrapped under a fresh recovery key, so
    every already-uploaded blob stays readable with no re-encryption.
    """
    vault_doc = await load_vault_doc(store, team_id)

    if vault_doc is None:
        legacy = legacy_sync_key()
        if legacy is not None:
            recovery_key = recoverykey.generate()
            doc = {
                "vault_format": VAULT_FORMAT,
                "team_id": team_id,
                "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "migrated_from": "CATAPULT_SYNC_KEY",
                "wrap": wrap_data_key(legacy, recovery_key, team_id),
            }
            await store.put(_vault_key(team_id), json.dumps(doc, indent=2).encode("utf-8"))
            cache_recovery_key(team_id, recovery_key)
            logger.info("Adopted the legacy sync key into a recovery-key vault")
            return "ok", legacy
        return "needs_setup", None

    recovery_key = cached_recovery_key(team_id)
    if recovery_key is None:
        legacy = legacy_sync_key()
        if legacy is not None and vault_doc.get("migrated_from") == "CATAPULT_SYNC_KEY":
            # Another Mac already migrated the shared CATAPULT_SYNC_KEY. That
            # key IS the data key, so this Mac keeps syncing without the
            # recovery key the other Mac minted (Settings can show it there).
            return "ok", legacy
        return "locked", None
    try:
        return "ok", unwrap_data_key(vault_doc, recovery_key, team_id)
    except WrongRecoveryKey:
        return "wrong_key", None


async def create_vault(apple_id: str, team_id: str, *, replace: bool = False) -> dict:
    """Create a new vault and return the recovery key ONCE, for display.

    Refuses to overwrite an existing vault unless ``replace`` is set. The
    descriptor holds the only wrap of the data key: overwriting it silently
    locked every other Mac out and left the old manifest undecryptable, so
    every later sync reported ``wrong_key``. Replacing moves the old vault
    aside first so the next sync starts from an empty manifest and re-uploads
    from this Mac's local vault.
    """
    config = SyncConfig.load()
    store = _store_from_config(config)
    if store is None:
        return {"status": "disabled", "message": "Choose where the vault should live first."}

    note = ""
    if await store.exists(_vault_key(team_id)):
        if not replace:
            return {
                "status": "exists",
                "message": (
                    "A vault already exists here. Unlock it with its recovery key, "
                    "or choose to start a new vault to replace it."
                ),
            }
        note = await store.archive_team(team_id)
        logger.warning("Replacing the sync vault for team %s. %s", team_id, note)

    doc, _data_key, recovery_key = new_vault(team_id)
    await store.put(_vault_key(team_id), json.dumps(doc, indent=2).encode("utf-8"))
    cache_recovery_key(team_id, recovery_key)
    logger.info("Created a new sync vault for team %s", team_id)
    message = "Save this recovery key. Catapult cannot recover it for you."
    return {
        "status": "ok",
        "recovery_key": recoverykey.encode(recovery_key),
        "message": f"{message} {note}".strip(),
    }


def recovery_key_for_display(team_id: str) -> str | None:
    """The recovery key this Mac already holds, encoded for showing on demand."""
    key = cached_recovery_key(team_id)
    return recoverykey.encode(key) if key is not None else None


async def unlock_vault(team_id: str, entered: str) -> dict:
    """Try a user-supplied recovery key against the remote vault."""
    config = SyncConfig.load()
    store = _store_from_config(config)
    if store is None:
        return {"status": "disabled", "message": "Sync is not configured."}

    vault_doc = await load_vault_doc(store, team_id)
    if vault_doc is None:
        return {"status": "needs_setup", "message": "There is no vault here yet."}

    try:
        key = recoverykey.decode(entered)
    except ValueError as e:
        return {"status": "invalid", "message": str(e)}

    try:
        unwrap_data_key(vault_doc, key, team_id)
    except WrongRecoveryKey as e:
        return {"status": "wrong_key", "message": str(e)}

    cache_recovery_key(team_id, key)
    return {"status": "ok", "message": "Vault unlocked."}


async def _upload_blob(store: RemoteStore, key: bytes, remote_key: str, source: Path) -> None:
    """Encrypt an IPA to a temp file and upload it, a chunk at a time."""
    staging = Path(tempfile.mkdtemp(prefix="catapult-upload-"))
    try:
        encrypted = staging / "blob.enc"
        encrypt_file(key, source, encrypted)
        await store.put_file(remote_key, encrypted)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def _download_blob(store: RemoteStore, key: bytes, team_id: str, digest: str) -> bool:
    """Fetch and decrypt a blob into the durable vault. Verifies the digest."""
    staging = Path(tempfile.mkdtemp(prefix="catapult-download-"))
    try:
        encrypted = staging / "blob.enc"
        if not await store.get_file(_ipa_key(team_id, digest), encrypted):
            return False
        plain = staging / "blob.ipa"
        decrypt_file(key, encrypted, plain)
        if vault.sha256_file(plain) != digest:
            raise ValueError(f"Downloaded IPA hash mismatch for {digest}")
        dest = vault.vault_path(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plain), str(dest))
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def sync_state(apple_id: str, team_id: str) -> dict:
    """Merge local state with remote manifest and sync encrypted IPA blobs."""
    config = SyncConfig.load()
    store = _store_from_config(config)
    if store is None:
        return {
            "status": "disabled",
            "provider": config.provider,
            "configured": config.configured,
        }

    vault_state, key = await open_vault(store, team_id)
    if vault_state != "ok" or key is None:
        return {
            "status": vault_state,
            "vault_state": vault_state,
            "provider": config.provider,
            "configured": True,
            "message": {
                "needs_setup": "Set up the vault to start syncing.",
                "locked": "Enter your recovery key on this Mac to unlock the vault.",
                "wrong_key": "That recovery key does not open this vault.",
            }.get(vault_state, ""),
        }

    local_state = load_state()
    local_installs = local_state.get("installs", [])
    remote_payload = await store.get(_manifest_key(team_id))
    try:
        remote_manifest = (
            _decrypt_json(key, remote_payload)
            if remote_payload
            else _default_manifest(apple_id, team_id)
        )
    except InvalidTag:
        # The vault descriptor unwrapped but the manifest will not decrypt, so
        # the vault and the manifest were written with different data keys.
        return {
            "status": "wrong_key",
            "vault_state": "wrong_key",
            "provider": config.provider,
            "configured": True,
            "portable_key": True,
            "message": "The stored manifest does not match this vault's key.",
        }
    remote_installs = remote_manifest.get("installs", [])
    merged_installs = _merge_installs(local_installs, remote_installs)

    downloaded = 0
    uploaded = 0
    for record in merged_installs:
        digest = record.get("ipa_sha256", "")
        if not digest:
            continue
        local_ipa = vault.resolve_ipa_path(record)
        if local_ipa is None:
            if await _download_blob(store, key, team_id, digest):
                record["ipa_path"] = str(vault.vault_path(digest))
                downloaded += 1
        else:
            record["ipa_path"] = str(local_ipa)
            remote_ipa_key = _ipa_key(team_id, digest)
            if not await store.exists(remote_ipa_key):
                await _upload_blob(store, key, remote_ipa_key, local_ipa)
                uploaded += 1

    local_state["installs"] = merged_installs
    save_state(local_state)

    manifest = _default_manifest(apple_id, team_id)
    manifest["installs"] = merged_installs
    await store.put(_manifest_key(team_id), _encrypt_json(key, manifest))

    return {
        "status": "ok",
        "vault_state": "ok",
        "provider": config.provider,
        "configured": True,
        # Retained for one release so the existing status row keeps compiling.
        "portable_key": True,
        "uploaded_ipas": uploaded,
        "downloaded_ipas": downloaded,
        "install_count": len(merged_installs),
        "vault_bytes": _vault_bytes(),
    }


def _vault_bytes() -> int:
    """Local size of the IPA vault, for the quota line in the UI."""
    try:
        return sum(p.stat().st_size for p in vault.IPA_VAULT_DIR.glob("*.ipa"))
    except OSError:
        return 0


def _folder_vault_state(folder: Path, team_id: str) -> str:
    """Vault state for a folder provider, from the descriptor on disk."""
    descriptor = FolderStore(folder)._path(_vault_key(team_id))
    legacy = legacy_sync_key()
    if not descriptor.exists():
        # No vault yet. A legacy CATAPULT_SYNC_KEY is adopted into a new vault
        # on the next sync run, so that Mac is effectively ready.
        return "ok" if legacy is not None else "needs_setup"
    if cached_recovery_key(team_id) is not None:
        return "ok"
    try:
        doc = json.loads(descriptor.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        doc = {}
    if legacy is not None and doc.get("migrated_from") == "CATAPULT_SYNC_KEY":
        return "ok"
    return "locked"


def status(apple_id: str = "", team_id: str = "") -> dict:
    """Configuration snapshot. Never performs network I/O."""
    config = SyncConfig.load()
    # A legacy CATAPULT_SYNC_KEY counts: open_vault() adopts it on the next run,
    # so reporting "locked" here would send the user hunting for a recovery key
    # they do not need yet.
    have_key = (bool(team_id) and cached_recovery_key(team_id) is not None) or (
        legacy_sync_key() is not None
    )
    if config.provider == "disabled" or not config.configured:
        vault_state = "disabled"
    elif config.provider == "folder" and config.folder == ICLOUD_VAULT_PATH and not icloud_drive_available():
        vault_state = "needs_icloud"
    elif config.provider == "folder" and config.folder and team_id:
        # A folder vault is plain file I/O, so the descriptor can be consulted
        # here without network access. This is what lets a first Mac see
        # "needs_setup" (and the Create vault button) instead of "locked".
        vault_state = _folder_vault_state(config.folder, team_id)
    else:
        vault_state = "ok" if have_key else "locked"

    return {
        "provider": config.provider,
        "configured": config.configured,
        "vault_state": vault_state,
        "vault_bytes": _vault_bytes(),
        "icloud_available": icloud_drive_available(),
        "icloud_path": str(ICLOUD_VAULT_PATH),
        "have_recovery_key": have_key,
        "portable_key": have_key,
        "folder": str(config.folder) if config.folder else "",
        "r2_endpoint": config.r2_endpoint,
        "r2_bucket": config.r2_bucket,
        "apple_id": apple_id,
        "team_id": team_id,
    }
