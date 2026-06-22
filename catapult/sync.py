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
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from catapult import vault
from catapult.refresh import (
    _keychain_get,
    _keychain_set,
    load_state,
    save_state,
)

logger = logging.getLogger(__name__)

SYNC_KEYCHAIN_SERVICE_ACCOUNT_PREFIX = "sync-key"
SYNC_MANIFEST_VERSION = 1


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
        provider = os.environ.get("CATAPULT_SYNC_PROVIDER", "disabled").strip().lower()
        folder = os.environ.get("CATAPULT_SYNC_FOLDER", "").strip()
        return cls(
            provider=provider or "disabled",
            folder=Path(folder).expanduser() if folder else None,
            r2_endpoint=os.environ.get("CATAPULT_R2_ENDPOINT", "").strip().rstrip("/"),
            r2_bucket=os.environ.get("CATAPULT_R2_BUCKET", "").strip(),
            r2_access_key_id=os.environ.get("CATAPULT_R2_ACCESS_KEY_ID", "").strip(),
            r2_secret_access_key=os.environ.get("CATAPULT_R2_SECRET_ACCESS_KEY", "").strip(),
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
    env_key = os.environ.get("CATAPULT_SYNC_KEY", "")
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
    return b"catapult-sync-v1\n" + nonce + ciphertext


def _decrypt_bytes(key: bytes, payload: bytes) -> bytes:
    prefix = b"catapult-sync-v1\n"
    if not payload.startswith(prefix):
        raise ValueError("Unsupported encrypted sync payload")
    body = payload[len(prefix):]
    nonce = body[:12]
    ciphertext = body[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None)


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
    if not os.environ.get("CATAPULT_SYNC_KEY", ""):
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
    portable_key = bool(os.environ.get("CATAPULT_SYNC_KEY", ""))
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
