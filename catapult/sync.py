"""Encrypted cross-device sync for Catapult install records and IPA blobs."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
import secrets
import shlex
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
CONFIG_ENV_PATH = Path.home() / ".catapult" / "config.env"


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


def get_sync_key(apple_id: str, team_id: str) -> tuple[bytes, bool]:
    """Return (key, portable).

    portable=True means the key came from CATAPULT_SYNC_KEY and can be reused on
    another Mac by setting the same value. Keychain-generated keys are local.
    """
    env_key = _sync_setting("CATAPULT_SYNC_KEY")
    if env_key:
        return _normalize_key(env_key), True

    account = _sync_key_account(apple_id, team_id)
    saved = _keychain_get(account)
    if saved:
        return _normalize_key(saved), False

    key = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")
    if not _keychain_set(account, encoded):
        logger.warning("Could not persist generated sync key in Keychain")
    return key, False


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


class FolderStore(RemoteStore):
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        cleaned = key.strip("/")
        return self.root / cleaned

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.exists():
            return None
        return path.read_bytes()

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()


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


def _store_from_config(config: SyncConfig) -> RemoteStore | None:
    if not config.configured:
        return None
    if config.provider == "folder" and config.folder:
        return FolderStore(config.folder)
    if config.provider == "r2":
        return R2Store(config)
    return None


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


async def sync_state(apple_id: str, team_id: str) -> dict:
    """Merge local state with remote manifest and sync encrypted IPA blobs."""
    config = SyncConfig.from_env()
    store = _store_from_config(config)
    if store is None:
        return {
            "status": "disabled",
            "provider": config.provider,
            "configured": config.configured,
        }
    if not _sync_setting("CATAPULT_SYNC_KEY"):
        return {
            "status": "needs_key",
            "provider": config.provider,
            "configured": True,
            "portable_key": False,
            "message": "Set CATAPULT_SYNC_KEY on every Mac that should decrypt this IPA vault.",
        }

    key, portable_key = get_sync_key(apple_id, team_id)
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
        return {
            "status": "wrong_key",
            "provider": config.provider,
            "configured": True,
            "portable_key": portable_key,
            "message": "The configured CATAPULT_SYNC_KEY cannot decrypt this remote vault.",
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
            encrypted = await store.get(_ipa_key(team_id, digest))
            if encrypted:
                data = _decrypt_bytes(key, encrypted)
                dest = vault.vault_path(digest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".ipa.tmp")
                tmp.write_bytes(data)
                if vault.sha256_file(tmp) != digest:
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"Downloaded IPA hash mismatch for {digest}")
                tmp.replace(dest)
                record["ipa_path"] = str(dest)
                downloaded += 1
        else:
            record["ipa_path"] = str(local_ipa)
            remote_ipa_key = _ipa_key(team_id, digest)
            if not await store.exists(remote_ipa_key):
                await store.put(remote_ipa_key, _encrypt_bytes(key, local_ipa.read_bytes()))
                uploaded += 1

    local_state["installs"] = merged_installs
    save_state(local_state)

    manifest = _default_manifest(apple_id, team_id)
    manifest["installs"] = merged_installs
    await store.put(_manifest_key(team_id), _encrypt_json(key, manifest))

    return {
        "status": "ok",
        "provider": config.provider,
        "configured": True,
        "portable_key": portable_key,
        "uploaded_ipas": uploaded,
        "downloaded_ipas": downloaded,
        "install_count": len(merged_installs),
    }


def status(apple_id: str = "", team_id: str = "") -> dict:
    config = SyncConfig.from_env()
    portable_key = bool(_sync_setting("CATAPULT_SYNC_KEY"))
    return {
        "provider": config.provider,
        "configured": config.configured,
        "portable_key": portable_key,
        "folder": str(config.folder) if config.folder else "",
        "r2_endpoint": config.r2_endpoint,
        "r2_bucket": config.r2_bucket,
        "apple_id": apple_id,
        "team_id": team_id,
    }
