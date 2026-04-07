"""Persistent install records and background 7-day refresh scheduler."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".catapult"
STATE_FILE = STATE_DIR / "state.json"

REFRESH_INTERVAL_DAYS = 7
# Re-sign 12h before expiry to be safe
REFRESH_AFTER_SECONDS = (REFRESH_INTERVAL_DAYS - 0.5) * 86400


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


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


def record_install(device_udid: str, ipa_path: str, device_name: str = ""):
    state = load_state()
    installs = state.get("installs", [])
    # Update existing or add new
    for rec in installs:
        if rec["device_udid"] == device_udid and rec["ipa_path"] == ipa_path:
            rec["last_installed"] = _now_ts()
            rec["device_name"] = device_name or rec.get("device_name", "")
            save_state(state)
            return
    installs.append({
        "device_udid": device_udid,
        "ipa_path": ipa_path,
        "device_name": device_name,
        "last_installed": _now_ts(),
    })
    state["installs"] = installs
    save_state(state)
    logger.info("Recorded install: %s → %s", ipa_path, device_udid)


def save_session(session) -> None:
    """Persist auth session tokens to disk."""
    state = load_state()
    state["session"] = {
        "apple_id": session.apple_id,
        "adsid": session.adsid,
        "dsprsid": session.dsprsid,
        "idms_token": session.idms_token,
        "gs_token": session.gs_token,
        "sk": session.sk.hex() if session.sk else "",
        "c": session.c.hex() if session.c else "",
        "authenticated": session.authenticated,
    }
    save_state(state)


def restore_session(auth_client) -> bool:
    """Restore saved session into auth_client. Returns True if restored."""
    state = load_state()
    saved = state.get("session")
    if not saved or not saved.get("authenticated"):
        return False
    from catapult.apple_auth import AuthSession
    s = AuthSession(
        apple_id=saved.get("apple_id", ""),
        adsid=saved.get("adsid", ""),
        dsprsid=saved.get("dsprsid", ""),
        idms_token=saved.get("idms_token", ""),
        gs_token=saved.get("gs_token", ""),
        sk=bytes.fromhex(saved.get("sk", "")) if saved.get("sk") else b"",
        c=bytes.fromhex(saved.get("c", "")) if saved.get("c") else b"",
        authenticated=True,
    )
    auth_client.session = s
    logger.info("Restored session for %s", s.apple_id)
    return True


def due_installs(state: dict) -> list[dict]:
    """Return installs that need refreshing."""
    now = _now_ts()
    return [
        r for r in state.get("installs", [])
        if now - r.get("last_installed", 0) >= REFRESH_AFTER_SECONDS
    ]


async def run_refresh_loop(get_server_components):
    """Background loop — refreshes installs every ~7 days."""
    CHECK_INTERVAL = 3600  # check every hour
    await asyncio.sleep(30)  # let server settle on startup

    while True:
        try:
            state = load_state()
            pending = due_installs(state)
            if pending:
                logger.info("Auto-refresh: %d install(s) due", len(pending))
                device_manager, auth_client, dev_services, signer, ipa_processor = get_server_components()
                session = auth_client.session
                if not session or not session.authenticated:
                    logger.warning("Auto-refresh skipped — not authenticated")
                else:
                    for rec in pending:
                        await _refresh_install(rec, device_manager, auth_client,
                                               dev_services, signer, ipa_processor)
        except Exception:
            logger.exception("Auto-refresh error")

        await asyncio.sleep(CHECK_INTERVAL)


async def _refresh_install(rec, device_manager, auth_client, dev_services, signer, ipa_processor):
    """Re-sign and re-install a single recorded app."""
    device_udid = rec["device_udid"]
    ipa_path = rec["ipa_path"]
    logger.info("Refreshing %s on %s", ipa_path, device_udid)
    try:
        session = auth_client.session
        team = await dev_services.get_team(session)
        team_id = team["teamId"]
        cert, private_key = await dev_services.get_or_create_cert(session, team_id)

        real_udid, sub_platform = await device_manager.get_real_udid()
        await dev_services.register_device(session, team_id, real_udid, rec.get("device_name", ""))

        ipa_info = await ipa_processor.inspect(ipa_path)
        original_bundle_id = ipa_info["bundle_id"]
        sideload_bundle_id = dev_services.sideload_bundle_id(team_id, original_bundle_id)
        app_id = await dev_services.register_app_id(session, team_id, original_bundle_id)
        profile = await dev_services.create_profile(
            session, team_id, app_id, cert, real_udid, sub_platform=sub_platform
        )
        signed_path = await signer.sign(ipa_path, cert, private_key, profile, sideload_bundle_id)
        await device_manager.install(device_udid, signed_path)

        record_install(device_udid, ipa_path, rec.get("device_name", ""))
        logger.info("Auto-refresh complete: %s", ipa_path)
    except Exception:
        logger.exception("Auto-refresh failed for %s", ipa_path)
