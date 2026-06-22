"""Persistent install records and opportunistic background refresh scheduler."""

import asyncio
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from catapult.errors import normalize_error
from catapult import vault

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".catapult"
STATE_FILE = STATE_DIR / "state.json"
_KEYCHAIN_SERVICE = "com.catapult.session"

REFRESH_INTERVAL_DAYS = 7
REFRESH_VALID_SECONDS = REFRESH_INTERVAL_DAYS * 86400
REFRESH_WINDOW_HOURS = 72
REFRESH_WINDOW_SECONDS = REFRESH_WINDOW_HOURS * 3600
# Re-sign opportunistically once an app has 72h or less before expiry.
REFRESH_AFTER_SECONDS = max(0, REFRESH_VALID_SECONDS - REFRESH_WINDOW_SECONDS)


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _expiry_ts(last_installed: float | int | None) -> float | None:
    if not last_installed:
        return None
    return float(last_installed) + REFRESH_VALID_SECONDS


def _refresh_after_ts(last_installed: float | int | None) -> float | None:
    if not last_installed:
        return None
    return float(last_installed) + REFRESH_AFTER_SECONDS


def seconds_until_expiry(rec: dict, now: float | None = None) -> float | None:
    expires_at = _expiry_ts(rec.get("last_installed"))
    if expires_at is None:
        return None
    return expires_at - (now if now is not None else _now_ts())


def seconds_until_refresh(rec: dict, now: float | None = None) -> float | None:
    refresh_after = _refresh_after_ts(rec.get("last_installed"))
    if refresh_after is None:
        return None
    return refresh_after - (now if now is not None else _now_ts())


def _stamp_refresh_schedule(rec: dict, installed_at: float) -> None:
    rec["last_installed"] = installed_at
    rec["expires_at"] = _expiry_ts(installed_at)
    rec["refresh_after"] = _refresh_after_ts(installed_at)
    rec["refresh_window_hours"] = REFRESH_WINDOW_HOURS


def load_state() -> dict:
    STATE_DIR.mkdir(exist_ok=True)
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"session": None, "installs": []}


def save_state(state: dict):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def record_install(
    device_udid: str,
    ipa_path: str,
    device_name: str = "",
    bundle_id: str = "",
    source_bundle_id: str = "",
    app_name: str = "",
    ipa_sha256: str = "",
    ipa_size: int | None = None,
    original_filename: str = "",
):
    state = load_state()
    installs = state.get("installs", [])
    installed_at = _now_ts()
    if ipa_path:
        try:
            vaulted = vault.store_ipa(ipa_path, original_filename=original_filename)
            ipa_path = vaulted["path"]
            ipa_sha256 = ipa_sha256 or vaulted["sha256"]
            ipa_size = ipa_size or vaulted["size"]
            original_filename = original_filename or vaulted["original_filename"]
        except Exception:
            logger.debug("Could not import IPA into durable vault: %s", ipa_path, exc_info=True)

    # Update existing or add new. A user can choose the same IPA from a new path
    # later; keep one refresh record per app/device so the durable path replaces
    # older temporary upload paths.
    for rec in installs:
        same_device = rec.get("device_udid") == device_udid
        same_file = same_device and rec.get("ipa_path") == ipa_path
        same_hash = same_device and ipa_sha256 and rec.get("ipa_sha256") == ipa_sha256
        same_signed_bundle = same_device and bundle_id and rec.get("bundle_id") == bundle_id
        same_source_bundle = same_device and source_bundle_id and rec.get("source_bundle_id") == source_bundle_id
        if same_file or same_hash or same_signed_bundle or same_source_bundle:
            rec["ipa_path"] = ipa_path
            _stamp_refresh_schedule(rec, installed_at)
            rec["device_name"] = device_name or rec.get("device_name", "")
            if bundle_id:
                rec["bundle_id"] = bundle_id
            if source_bundle_id:
                rec["source_bundle_id"] = source_bundle_id
            if app_name:
                rec["app_name"] = app_name
            if ipa_sha256:
                rec["ipa_sha256"] = ipa_sha256
            if ipa_size is not None:
                rec["ipa_size"] = ipa_size
            if original_filename:
                rec["original_filename"] = original_filename
            rec["fail_count"] = 0
            save_state(state)
            return
    installs.append({
        "device_udid": device_udid,
        "ipa_path": ipa_path,
        "device_name": device_name,
        "bundle_id": bundle_id,
        "source_bundle_id": source_bundle_id,
        "app_name": app_name,
        "ipa_sha256": ipa_sha256,
        "ipa_size": ipa_size,
        "original_filename": original_filename,
        "last_installed": installed_at,
        "expires_at": _expiry_ts(installed_at),
        "refresh_after": _refresh_after_ts(installed_at),
        "refresh_window_hours": REFRESH_WINDOW_HOURS,
    })
    state["installs"] = installs
    save_state(state)
    logger.info("Recorded install: %s → %s", ipa_path, device_udid)


def find_recorded_bundle_id(device_udid: str, candidate_bundle_ids: list[str]) -> str | None:
    """Return a prior installed bundle ID for this device/app, if known."""
    candidates = {value for value in candidate_bundle_ids if value}
    if not candidates:
        return None
    state = load_state()
    for rec in state.get("installs", []):
        if rec.get("device_udid") != device_udid:
            continue
        bundle_id = rec.get("bundle_id") or ""
        if bundle_id in candidates:
            return bundle_id
    return None


def _keychain_set(account: str, data: str) -> bool:
    """Store a value in macOS Keychain."""
    # Delete existing entry first (ignore errors if not found)
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
    )
    result = subprocess.run(
        ["security", "add-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account,
         "-w", data, "-U"],
        capture_output=True,
    )
    return result.returncode == 0


def _keychain_get(account: str) -> str | None:
    """Retrieve a value from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _keychain_delete(account: str):
    """Remove a value from macOS Keychain."""
    subprocess.run(
        ["security", "delete-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", account],
        capture_output=True,
    )


def save_session(session) -> None:
    """Persist auth session — tokens go to Keychain, metadata to state file."""
    tokens = json.dumps({
        "adsid": session.adsid,
        "dsprsid": session.dsprsid,
        "idms_token": session.idms_token,
        "gs_token": session.gs_token,
        "sk": session.sk.hex() if session.sk else "",
        "c": session.c.hex() if session.c else "",
    })
    if _keychain_set(session.apple_id, tokens):
        logger.info("Session tokens stored in Keychain")
    else:
        logger.warning("Failed to store tokens in Keychain, falling back to file")

    state = load_state()
    state["session"] = {
        "apple_id": session.apple_id,
        "authenticated": session.authenticated,
    }
    save_state(state)


def restore_session(auth_client) -> bool:
    """Restore saved session — read tokens from Keychain."""
    state = load_state()
    saved = state.get("session")
    if not saved or not saved.get("authenticated"):
        return False

    apple_id = saved.get("apple_id", "")
    raw = _keychain_get(apple_id)
    if not raw:
        logger.warning("No Keychain entry for %s", apple_id)
        return False

    try:
        tokens = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Corrupt Keychain data for %s", apple_id)
        return False

    from catapult.apple_auth import AuthSession
    s = AuthSession(
        apple_id=apple_id,
        adsid=tokens.get("adsid", ""),
        dsprsid=tokens.get("dsprsid", ""),
        idms_token=tokens.get("idms_token", ""),
        gs_token=tokens.get("gs_token", ""),
        sk=bytes.fromhex(tokens.get("sk", "")) if tokens.get("sk") else b"",
        c=bytes.fromhex(tokens.get("c", "")) if tokens.get("c") else b"",
        authenticated=True,
    )
    auth_client.session = s
    logger.info("Restored session for %s (from Keychain)", s.apple_id)
    return True


MAX_CONSECUTIVE_FAILURES = 3


def _rewrite_nested_bundle_id(parent_old_id: str, parent_new_id: str, nested_old_id: str) -> str:
    if nested_old_id == parent_old_id:
        return parent_new_id
    if nested_old_id.startswith(parent_old_id + "."):
        return parent_new_id + nested_old_id[len(parent_old_id):]
    return f"{parent_new_id}.{nested_old_id.rsplit('.', 1)[-1]}"


def due_installs(state: dict) -> list[dict]:
    """Return installs that need refreshing (skip those that failed too many times)."""
    now = _now_ts()
    return [
        r for r in state.get("installs", [])
        if (
            seconds_until_expiry(r, now) is None
            or seconds_until_expiry(r, now) <= REFRESH_WINDOW_SECONDS
        )
        and r.get("fail_count", 0) < MAX_CONSECUTIVE_FAILURES
    ]


async def run_refresh_loop(get_server_components):
    """Background loop — refreshes installs once they enter the 72h expiry window."""
    CHECK_INTERVAL = 3600  # check every hour
    await asyncio.sleep(30)  # let server settle on startup

    while True:
        try:
            state = load_state()
            pending = due_installs(state)
            if pending:
                logger.info("Auto-refresh: %d install(s) in the %dh refresh window", len(pending), REFRESH_WINDOW_HOURS)
                components = tuple(get_server_components())
                device_manager, auth_client, dev_services, signer, ipa_processor = components[:5]
                activity_manager = components[5] if len(components) > 5 else None
                session = auth_client.session
                if not session or not session.authenticated:
                    logger.warning("Auto-refresh skipped — not authenticated")
                    if activity_manager:
                        job = activity_manager.start(
                            "refresh",
                            "Auto-refresh skipped",
                            target=f"{len(pending)} install(s)",
                            message="Not authenticated.",
                        )
                        normalized = normalize_error("Not authenticated. Please sign in first.")
                        activity_manager.fail(
                            job,
                            normalized.message,
                            category=normalized.category,
                            detail=normalized.detail,
                        )
                else:
                    for rec in pending:
                        job = None
                        if activity_manager:
                            job = activity_manager.start(
                                "refresh",
                                f"Refresh {rec.get('app_name') or Path(rec.get('ipa_path', '')).name or 'app'}",
                                target=rec.get("device_name") or rec.get("device_udid", ""),
                                message="Refreshing saved install...",
                            )
                        result = await _refresh_install(rec, device_manager, auth_client,
                                                        dev_services, signer, ipa_processor)
                        if activity_manager and job:
                            if result.get("status") == "ok":
                                activity_manager.complete(
                                    job,
                                    message=result.get("message") or "Auto-refresh complete.",
                                )
                            else:
                                normalized = normalize_error(result.get("message") or "Auto-refresh failed.")
                                activity_manager.fail(
                                    job,
                                    normalized.message,
                                    category=normalized.category,
                                    detail=normalized.detail,
                                )
        except Exception:
            logger.exception("Auto-refresh error")

        await asyncio.sleep(CHECK_INTERVAL)


async def _refresh_install(rec, device_manager, auth_client, dev_services, signer, ipa_processor):
    """Re-sign and re-install a single recorded app."""
    device_udid = rec["device_udid"]
    ipa_path = rec["ipa_path"]
    logger.info("Refreshing %s on %s", ipa_path, device_udid)
    try:
        ipa_file = vault.resolve_ipa_path(rec)
        if ipa_file is None:
            missing = ipa_path or rec.get("ipa_sha256", "")
            raise RuntimeError(f"IPA file is missing: {missing}. Choose the IPA again before refreshing.")

        session = auth_client.session
        team = await dev_services.get_team(session)
        team_id = team["teamId"]
        cert, private_key = await dev_services.get_or_create_cert(session, team_id)

        device_info = await device_manager.get_device_info(device_udid)
        if "remotepairing" in device_info.get("service", ""):
            tunnel = await device_manager.start_tunnel(
                device_udid=device_udid,
                device_host=device_info.get("host", ""),
            )
            if tunnel.get("status") != "ok":
                raise RuntimeError(tunnel.get("message") or "Apple TV tunnel is not ready.")
        if device_info.get("service") == "usbmux":
            real_udid = device_info["udid"]
            sub_platform = None
        else:
            real_udid, sub_platform = await device_manager.get_real_udid(
                device_udid=device_udid,
                device_host=device_info.get("host", ""),
            )
        await dev_services.register_device(
            session,
            team_id,
            real_udid,
            device_info.get("name") or rec.get("device_name", ""),
        )

        ipa_info = await ipa_processor.inspect(str(ipa_file))
        bundle_id = rec.get("bundle_id") or ipa_info["bundle_id"]
        app_id = await dev_services.register_app_id(session, team_id, bundle_id)
        profile = await dev_services.create_profile(
            session, team_id, app_id, cert, real_udid, sub_platform=sub_platform
        )
        rewrite_bundle_id = bundle_id if bundle_id != ipa_info["bundle_id"] else None

        extension_profiles: dict[str, bytes] = {}
        if rewrite_bundle_id:
            for extension in await ipa_processor.inspect_extensions(str(ipa_file)):
                extension_bundle_id = extension.get("bundle_id", "")
                if not extension_bundle_id:
                    continue
                target_extension_bundle_id = _rewrite_nested_bundle_id(
                    ipa_info["bundle_id"],
                    bundle_id,
                    extension_bundle_id,
                )
                extension_app_id = await dev_services.register_app_id(
                    session,
                    team_id,
                    target_extension_bundle_id,
                )
                extension_profiles[target_extension_bundle_id] = await dev_services.create_profile(
                    session,
                    team_id,
                    extension_app_id,
                    cert,
                    real_udid,
                    sub_platform=sub_platform,
                )

        signed_path = await signer.sign(
            str(ipa_file),
            cert,
            private_key,
            profile,
            rewrite_bundle_id,
            extension_profiles=extension_profiles,
        )
        await device_manager.install(device_udid, signed_path)

        # Success — reset failure count
        rec["fail_count"] = 0
        record_install(
            device_udid,
            str(ipa_file),
            device_info.get("name") or rec.get("device_name", ""),
            bundle_id=bundle_id,
            source_bundle_id=ipa_info.get("bundle_id", ""),
            app_name=ipa_info.get("bundle_name", ""),
            ipa_sha256=rec.get("ipa_sha256", ""),
            ipa_size=rec.get("ipa_size"),
            original_filename=rec.get("original_filename", ""),
        )
        logger.info("Auto-refresh complete: %s", ipa_path)
        return {"status": "ok", "message": "Auto-refresh complete."}
    except Exception as e:
        rec["fail_count"] = rec.get("fail_count", 0) + 1
        # Persist failure count
        state = load_state()
        for r in state.get("installs", []):
            if r["device_udid"] == device_udid and r["ipa_path"] == ipa_path:
                r["fail_count"] = rec["fail_count"]
                break
        save_state(state)
        logger.exception("Auto-refresh failed for %s (attempt %d/%d)",
                         ipa_path, rec["fail_count"], MAX_CONSECUTIVE_FAILURES)
        return {"status": "error", "message": str(e), "fail_count": rec["fail_count"]}
