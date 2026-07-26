"""FastAPI server — REST + WebSocket API for the Catapult UI."""

import asyncio
from collections import deque
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from catapult.apple_auth import AppleAuthClient
from catapult.developer import DeveloperServices
from catapult.device import DeviceManager
from catapult.errors import normalize_error, redact_sensitive
from catapult.ipa import IpaProcessor
from catapult.jobs import ActivityJob, job_manager
from catapult.signer import Signer
from catapult import refresh as _refresh
from catapult import sync as _sync
from catapult import vault as _vault

logger = logging.getLogger(__name__)

from starlette.responses import Response

app = FastAPI(title="Catapult")

def _static_dir() -> Path:
    """Resolve static/ dir — works both from source and inside a PyInstaller .app bundle."""
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "static"
    return Path(__file__).parent.parent / "static"


static_dir = _static_dir()
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

device_manager = DeviceManager()
auth_client = AppleAuthClient()
dev_services = DeveloperServices()
signer = Signer()
ipa_processor = IpaProcessor()
NATIVE_BACKEND_PROTOCOL = 10
DIAGNOSTICS_LOG_PATH = Path.home() / ".catapult" / "agent.log"


@app.on_event("startup")
async def _on_startup():
    # Restore saved session on startup
    restored = _refresh.restore_session(auth_client)
    if restored and auth_client.session:
        asyncio.create_task(_sync_authenticated_state())
    # Start opportunistic auto-refresh background loop.
    def _components():
        return device_manager, auth_client, dev_services, signer, ipa_processor, job_manager
    asyncio.create_task(_refresh.run_refresh_loop(_components))

    # Warm the first device scan off the request path: right after boot it
    # competes with session restore and tunneld startup and can run well past
    # the endpoints' deadlines, 504ing the UI's first refresh.
    async def _warm_scan():
        try:
            await device_manager.discover()
        except Exception:
            logger.debug("Startup warm scan failed", exc_info=True)
    asyncio.create_task(_warm_scan())


async def _sync_authenticated_state() -> dict:
    session = auth_client.session
    if not session or not session.authenticated:
        return {"status": "skipped", "message": "Not authenticated"}
    team = await dev_services.get_team(session)
    return await _sync_state_for_team(session.apple_id, team["teamId"])


async def _sync_state_for_team(apple_id: str, team_id: str) -> dict:
    try:
        result = await _sync.sync_state(apple_id, team_id)
        if result.get("status") == "ok":
            logger.info(
                "Sync complete via %s: %s installs, %s uploaded, %s downloaded",
                result.get("provider"),
                result.get("install_count"),
                result.get("uploaded_ipas"),
                result.get("downloaded_ipas"),
            )
        elif result.get("status") != "disabled":
            logger.info("Sync state: %s", result)
        return result
    except Exception as e:
        logger.exception("Cross-device sync failed")
        return {"status": "error", "message": str(e)}


def _asset_hash(filename: str) -> str:
    """Short content hash for cache busting."""
    path = static_dir / filename
    if path.exists():
        return hashlib.md5(path.read_bytes()).hexdigest()[:8]
    return "0"


# ── Pages ──


@app.get("/")
async def index():
    from starlette.responses import HTMLResponse
    html = (static_dir / "index.html").read_text()
    # Replace hardcoded ?v=N with content-based hashes
    html = html.replace("styles.css?v=2", f"styles.css?v={_asset_hash('styles.css')}")
    html = html.replace("app.js?v=2", f"app.js?v={_asset_hash('app.js')}")
    return HTMLResponse(html)


# ── REST API ──


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "app": "catapult",
        "protocol": NATIVE_BACKEND_PROTOCOL,
        "pid": os.getpid(),
    }


@app.get("/api/activity")
async def list_activity(limit: int = 50):
    return {"jobs": job_manager.recent(limit=limit)}


@app.get("/api/diagnostics")
async def diagnostics_bundle():
    return {
        "backend": {
            "status": "ok",
            "app": "catapult",
            "protocol": NATIVE_BACKEND_PROTOCOL,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
        },
        "account": await _diagnostics_account_summary(),
        "devices": await _diagnostics_devices(),
        "recent_jobs": job_manager.recent(limit=50),
        "log_tail": _log_tail(DIAGNOSTICS_LOG_PATH),
    }


async def _diagnostics_account_summary() -> dict:
    session = auth_client.session
    if not session or not session.authenticated:
        return {"authenticated": False}
    summary = {
        "authenticated": True,
        "apple_id": session.apple_id,
    }
    try:
        team = await dev_services.get_team(session)
        summary["team"] = {
            "name": team.get("name", ""),
            "team_id": team.get("teamId", ""),
            "type": team.get("type", ""),
            "status": team.get("status", ""),
        }
    except Exception as e:
        summary["team_error"] = normalize_error(e).to_dict()
    return summary


async def _diagnostics_devices() -> dict:
    try:
        devices = await asyncio.wait_for(device_manager.discover(), timeout=8)
        return {"status": "ok", "devices": devices}
    except Exception as e:
        return {
            "status": "error",
            "devices": [],
            "error": normalize_error(e).to_dict(),
        }


def _log_tail(path: Path, *, line_count: int = 200) -> dict:
    result = {
        "path": str(path),
        "exists": path.exists(),
        "lines": [],
    }
    if not path.exists():
        return result
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            result["lines"] = [redact_sensitive(line.rstrip("\n")) for line in deque(f, maxlen=line_count)]
    except Exception as e:
        result["error"] = normalize_error(e).to_dict()
    return result


def _error_payload(error: BaseException | str, *, status: str = "error") -> dict:
    normalized = normalize_error(error)
    payload = {"status": status}
    payload.update(normalized.to_dict())
    return payload


def _annotate_job(payload: dict, job: ActivityJob | None) -> dict:
    if job:
        payload["job_id"] = job.id
    return payload


@app.get("/api/devices")
async def list_devices():
    try:
        devices = await asyncio.wait_for(
            device_manager.discover(allow_stale=True), timeout=15
        )
        return {"devices": devices}
    except asyncio.TimeoutError:
        logger.exception("Device scan timed out")
        return JSONResponse({"devices": [], "error": "Device scan timed out"}, status_code=504)
    except Exception as e:
        logger.exception("Device scan failed")
        return JSONResponse({"devices": [], "error": str(e)}, status_code=500)


@app.post("/api/devices/pair")
async def pair_device(payload: dict = None):
    """Initiate device pairing — shows admin password dialog and PIN on device."""
    name = payload.get("name") if payload else None
    udid = payload.get("udid") if payload else None
    host = payload.get("host") if payload else None
    return await device_manager.pair_device(device_name=name, device_udid=udid, device_host=host)


@app.post("/api/devices/tunnel")
async def start_tunnel():
    """Start a tunnel to paired devices — shows admin password dialog."""
    job = job_manager.start("setup", "Start device tunnel", message="Starting tunnel...")
    try:
        result = await device_manager.start_tunnel()
        if result.get("status") == "ok":
            job_manager.update(job, progress=80, message="Rescanning devices...")
            # Rescan to find newly available devices
            await device_manager.discover()
            job_manager.complete(job, message=result.get("message") or "Tunnel ready.")
        else:
            job_manager.fail(job, result.get("message") or "Tunnel failed.")
        return _annotate_job(result, job)
    except Exception as e:
        logger.exception("Tunnel start failed")
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job(_error_payload(e), job), status_code=500)


@app.post("/api/devices/setup")
async def setup_device(payload: dict = None):
    """Pair if needed, then start a tunnel for a device."""
    name = payload.get("name") if payload else None
    udid = payload.get("udid") if payload else None
    host = payload.get("host") if payload else None
    job = job_manager.start(
        "setup",
        "Set up device",
        target=name or udid or host or "",
        message="Checking pairing state...",
    )

    try:
        selected = device_manager._selected_device(device_udid=udid, device_host=host)
        already_paired = selected is not None and device_manager._is_known_paired(selected)
        if not already_paired:
            job_manager.update(job, progress=20, message="Pairing device...")
            pair_result = await device_manager.pair_device(device_name=name, device_udid=udid, device_host=host)
            if pair_result.get("status") != "ok":
                job_manager.fail(job, pair_result.get("message") or "Device pairing failed.")
                return _annotate_job(pair_result, job)

        # After successful pairing, start tunnel
        job_manager.update(job, progress=55, message="Starting tunnel...")
        tunnel_result = await device_manager.start_tunnel(device_udid=udid, device_host=host)
        if tunnel_result.get("status") == "ok":
            job_manager.update(job, progress=80, message="Discovering tunneled device...")
            await asyncio.sleep(2)
            await device_manager.discover()
            if host:
                device_manager._tunneled_hosts.add(host)
                device_manager._remember_paired_device(
                    name=name,
                    host=host,
                    identifiers=[udid],
                    model=(selected or {}).get("model"),
                )
            # Mark the selected host and all remotepairing devices as installable
            # now that tunneld is up. The selected Apple TV may have come from a
            # companion-link/AirPlay row, while the installable row may come from a
            # different mDNS service or a changed pairing identifier after setup.
            for d in device_manager._cache.values():
                if d.get("needs_setup") or (host and d.get("host") == host):
                    d["installable"] = True
                    d["needs_setup"] = False
                    device_manager._tunneled_hosts.add(d["host"])
                    device_manager._remember_paired_device(
                        name=d.get("name"),
                        host=d.get("host"),
                        identifiers=[d.get("udid"), *list(device_manager._device_identifiers(d))],
                        model=d.get("model"),
                    )
            tunnel_result["message"] = "Connected. Tunnel ready." if already_paired else "Setup complete. Tunnel ready."
            job_manager.complete(job, message=tunnel_result["message"])
        else:
            job_manager.fail(job, tunnel_result.get("message") or "Device setup failed.")
        return _annotate_job(tunnel_result, job)
    except Exception as e:
        logger.exception("Device setup failed")
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job(_error_payload(e), job), status_code=500)


@app.post("/api/devices/pin")
async def submit_pin(payload: dict):
    """Submit the PIN shown on the device during pairing."""
    pin = payload.get("pin", "")
    if not pin:
        return JSONResponse({"status": "error", "message": "PIN is required"}, status_code=400)
    device_manager.submit_pin(pin)
    return {"status": "ok"}


@app.get("/api/devices/pair-status")
async def pair_status():
    """Check current pairing state."""
    return {"state": device_manager._pairing_state}


@app.get("/api/auth/status")
async def auth_status():
    """Check if there's an active authenticated session."""
    if auth_client.session and auth_client.session.authenticated:
        return {"authenticated": True, "apple_id": auth_client.session.apple_id}
    return {"authenticated": False}


@app.get("/api/sync/status")
async def sync_status():
    """Return cross-device sync configuration and account context."""
    session = auth_client.session
    if not session or not session.authenticated:
        return _sync.status()
    try:
        team = await dev_services.get_team(session)
        return _sync.status(session.apple_id, team.get("teamId", ""))
    except Exception:
        logger.debug("Could not include team info in sync status", exc_info=True)
        return _sync.status(session.apple_id, "")


@app.post("/api/sync/run")
async def run_sync():
    """Manually merge local state with the configured remote sync provider."""
    session = auth_client.session
    if not session or not session.authenticated:
        return JSONResponse({"status": "error", "message": "Not authenticated"}, status_code=401)
    result = await _sync_authenticated_state()
    status_code = 200 if result.get("status") in {"ok", "disabled", "skipped"} else 500
    return JSONResponse(result, status_code=status_code)


@app.post("/api/auth/logout")
async def logout():
    """Clear session and remove Keychain tokens."""
    if auth_client.session and auth_client.session.apple_id:
        _refresh._keychain_delete(auth_client.session.apple_id)
    auth_client.session = None
    state = _refresh.load_state()
    state["session"] = None
    _refresh.save_state(state)
    logger.info("Signed out")
    return {"status": "ok"}


@app.post("/api/auth/login")
async def login(payload: dict):
    apple_id = payload.get("apple_id", "")
    password = payload.get("password", "")
    if not apple_id or not password:
        return JSONResponse({"status": "error", "message": "Apple ID and password are required"}, status_code=400)
    try:
        result = await auth_client.authenticate(apple_id, password)
        if result.get("status") == "ok":
            return await _fetch_team(result)
        return result
    except Exception as e:
        logger.exception("Apple ID login failed")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/auth/2fa")
async def verify_2fa(payload: dict):
    code = payload.get("code", "")
    if not code:
        return JSONResponse({"status": "error", "message": "Code is required"}, status_code=400)
    try:
        result = await auth_client.submit_2fa(code)
        if result.get("status") == "ok":
            return await _fetch_team(result)
        return result
    except Exception as e:
        logger.exception("Apple ID 2FA failed")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


async def _fetch_team(auth_result: dict) -> dict:
    """Fetch development team right after successful auth."""
    try:
        session = auth_client.session
        team = await dev_services.get_team(session)
        logger.info("Team: %s (%s)", team.get("name"), team.get("teamId"))
        _refresh.save_session(session)
        auth_result["sync"] = await _sync_state_for_team(session.apple_id, team.get("teamId", ""))
        return auth_result
    except Exception as e:
        logger.error("Team fetch failed: %s", e)
        return {"status": "error", "message": f"Signed in but team fetch failed: {e}"}


@app.get("/api/account/info")
async def account_info():
    """Return team info, registered app IDs, profiles with expiry, and devices."""
    session = auth_client.session
    if not session or not session.authenticated:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    try:
        team = await dev_services.get_team(session)
        team_id = team["teamId"]
        sync_result = await _sync_state_for_team(session.apple_id, team_id)

        app_ids = await dev_services._list_app_ids(session, team_id)

        from datetime import datetime, timedelta
        now = datetime.now().astimezone()
        now_ts = now.timestamp()

        def _format_time_left(delta) -> str:
            seconds = max(0, int(delta.total_seconds()))
            if seconds <= 0:
                return "Expired"
            days, rem = divmod(seconds, 86400)
            hours, rem = divmod(rem, 3600)
            minutes = rem // 60
            if days:
                return f"{days}d {hours}h left"
            if hours:
                return f"{hours}h {minutes}m left"
            return f"{max(1, minutes)}m left"

        # Load install history and map by both preserved and legacy copied bundle IDs.
        # Also resolve real app names from the IPA files
        install_state = _refresh.load_state()
        install_records = install_state.get("installs", [])
        install_by_bundle: dict[str, dict] = {}  # sideload_id -> best record
        app_display_names: dict[str, str] = {}   # sideload_id -> real app name
        extension_metadata: dict[str, dict] = {}

        def _record_score(record: dict) -> tuple[float, int, int]:
            return (
                float(record.get("last_installed") or 0),
                1 if record.get("ipa_sha256") else 0,
                1 if _vault.resolve_ipa_path(record) is not None else 0,
            )

        def _remember_install(bundle_key: str, record: dict):
            if not bundle_key:
                return
            existing = install_by_bundle.get(bundle_key)
            if not existing or _record_score(record) > _record_score(existing):
                install_by_bundle[bundle_key] = record

        def _record_timing(record: dict | None) -> dict:
            installed_str = None
            days_left = None
            exp_str = None
            expires_at = None
            time_left = None
            auto_refresh_after = None
            auto_refresh_eligible = False
            is_expired = False

            if record:
                ts = record.get("last_installed")
                if ts:
                    installed_dt = datetime.fromtimestamp(ts).astimezone()
                    # Free accounts: profile expires 7 days after signing
                    expiry_dt = installed_dt + timedelta(days=7)
                    delta = expiry_dt - now
                    days_left = max(0, delta.days)
                    exp_str = expiry_dt.strftime("%b %d, %Y %H:%M")
                    expires_at = expiry_dt.isoformat()
                    time_left = _format_time_left(delta)
                    installed_str = installed_dt.strftime("%b %d, %Y %H:%M")
                    is_expired = delta.total_seconds() <= 0
                    refresh_after_ts = record.get("refresh_after") or (ts + _refresh.REFRESH_AFTER_SECONDS)
                    auto_refresh_dt = datetime.fromtimestamp(refresh_after_ts).astimezone()
                    auto_refresh_after = auto_refresh_dt.isoformat()
                    auto_refresh_eligible = refresh_after_ts <= now_ts

            return {
                "installed": installed_str,
                "days_left": days_left,
                "expiry": exp_str,
                "expires_at": expires_at,
                "time_left": time_left,
                "auto_refresh_after": auto_refresh_after,
                "auto_refresh_eligible": auto_refresh_eligible,
                "is_expired": is_expired,
            }

        def _install_row(
            *,
            name: str,
            identifier: str,
            app_id_id: str = "",
            is_catapult: bool = False,
            is_extension: bool = False,
            extension_info: dict | None = None,
            rec: dict | None = None,
            account_slot_exists: bool = True,
        ) -> dict:
            timing = _record_timing(rec)
            saved_ipa_exists = _vault.resolve_ipa_path(rec) is not None if rec else False
            reinstall_blocked_reason = None
            if is_extension:
                reinstall_blocked_reason = "Reinstall the parent app to refresh this extension."
            elif not rec:
                reinstall_blocked_reason = "No saved install was found. Install from an IPA first."
            elif not saved_ipa_exists:
                reinstall_blocked_reason = "Saved IPA is missing. Choose the IPA again before reinstalling."

            row_id = app_id_id or ":".join([
                "history",
                identifier,
                rec.get("device_udid", "") if rec else "",
                str(rec.get("last_installed", "")) if rec else "",
            ])

            return {
                "row_id": row_id,
                "name": name,
                "identifier": identifier,
                "app_id_id": app_id_id,
                "is_catapult": is_catapult,
                "is_extension": is_extension,
                "parent_identifier": extension_info.get("parent_identifier") if extension_info else None,
                "parent_name": extension_info.get("parent_name") if extension_info else None,
                "extension_name": extension_info.get("extension_name") if extension_info else None,
                **timing,
                "installed_device": rec.get("device_name", "") if rec else None,
                "saved_device_name": rec.get("device_name", "") if rec else None,
                "saved_ipa_exists": saved_ipa_exists,
                "reinstall_blocked_reason": reinstall_blocked_reason,
                "can_reinstall": bool(rec and saved_ipa_exists and not is_extension),
                "account_slot_exists": account_slot_exists,
                "history_only": not account_slot_exists,
            }

        for r in install_records:
            path = r.get("ipa_path", "")
            resolved_path = _vault.resolve_ipa_path(r)
            path_exists = resolved_path is not None
            display = r.get("app_name", "")
            original_id = r.get("source_bundle_id", "")
            sideload_id = ""
            recorded_id = r.get("bundle_id", "")
            if original_id:
                sideload_id = dev_services.sideload_bundle_id(team_id, original_id)
            for bundle_key in {original_id, sideload_id, recorded_id}:
                _remember_install(bundle_key, r)
                if display:
                    app_display_names[bundle_key] = display
            if not path_exists:
                continue
            try:
                info = await ipa_processor.inspect(resolved_path)
                original_id = info["bundle_id"]
                sideload_id = dev_services.sideload_bundle_id(team_id, original_id)
                recorded_id = r.get("bundle_id", "")
                # Keep the most recent install record per bundle ID.
                for bundle_key in {original_id, sideload_id, recorded_id}:
                    _remember_install(bundle_key, r)
                    # Use the IPA's display name (e.g. "Stremio") not Apple's generic name
                    display = info.get("bundle_name") or info.get("bundle_id", "")
                    if display:
                        app_display_names[bundle_key] = display

                for extension in await ipa_processor.inspect_extensions(resolved_path):
                    extension_id = extension.get("bundle_id", "")
                    if not extension_id:
                        continue
                    extension_name = extension.get("bundle_name") or extension_id.rsplit(".", 1)[-1]
                    for parent_id in {original_id, sideload_id, recorded_id}:
                        if not parent_id:
                            continue
                        if parent_id == original_id:
                            target_extension_id = extension_id
                        else:
                            target_extension_id = _rewrite_nested_bundle_id(
                                original_id,
                                parent_id,
                                extension_id,
                            )
                        extension_metadata[target_extension_id] = {
                            "extension_name": extension_name,
                            "parent_identifier": parent_id,
                            "parent_name": display,
                        }
            except Exception:
                pass

        catapult_prefix = f"com.catapult.{team_id}."
        apps = []
        live_identifiers: set[str] = set()
        for a in app_ids:
            identifier = a.get("identifier", "")
            if identifier:
                live_identifiers.add(identifier)
            apple_name = a.get("name", "")
            app_id_id = a.get("appIdId", "")
            is_catapult = identifier.startswith(catapult_prefix)

            # Use real app name from IPA if available, otherwise Apple's registered name
            name = app_display_names.get(identifier, apple_name)
            extension_info = extension_metadata.get(identifier)
            is_extension = extension_info is not None
            if is_extension:
                parent_name = extension_info.get("parent_name") or "App"
                extension_name = extension_info.get("extension_name") or "Extension"
                clean_extension = (
                    "Widget Extension"
                    if extension_name.lower() in {"altwidget", "widget", "widgetextension"}
                    else extension_name
                )
                name = f"{parent_name} {clean_extension}"

            # Find install record for this app
            rec = install_by_bundle.get(identifier)
            if not rec and is_extension:
                rec = install_by_bundle.get(extension_info.get("parent_identifier", ""))

            apps.append(_install_row(
                name=name,
                identifier=identifier,
                app_id_id=app_id_id,
                is_catapult=is_catapult,
                is_extension=is_extension,
                extension_info=extension_info,
                rec=rec,
                account_slot_exists=True,
            ))

        # Apple only reports current App IDs. Keep local install history visible
        # even after an app expires, the App ID is deleted, or another Mac syncs
        # the install record before Apple has recreated the slot.
        history_seen: set[tuple[str, str]] = set()
        for rec in sorted(install_records, key=lambda item: _record_score(item), reverse=True):
            identifier = (
                rec.get("bundle_id")
                or (
                    dev_services.sideload_bundle_id(team_id, rec["source_bundle_id"])
                    if rec.get("source_bundle_id")
                    else ""
                )
                or rec.get("source_bundle_id", "")
            )
            if not identifier or identifier in live_identifiers:
                continue
            history_key = (identifier, rec.get("device_udid", ""))
            if history_key in history_seen:
                continue
            history_seen.add(history_key)
            name = (
                rec.get("app_name")
                or app_display_names.get(identifier)
                or rec.get("source_bundle_id")
                or identifier
            )
            apps.append(_install_row(
                name=name,
                identifier=identifier,
                app_id_id="",
                is_catapult=identifier.startswith(catapult_prefix),
                is_extension=False,
                extension_info=None,
                rec=rec,
                account_slot_exists=False,
            ))

        apps.sort(
            key=lambda item: (
                1 if item.get("is_expired") else 0,
                (item.get("parent_name") or item.get("name") or item.get("identifier") or "").lower(),
                1 if item.get("is_extension") else 0,
                (item.get("name") or item.get("identifier") or "").lower(),
            )
        )

        team_type = team.get("type", "")
        is_free = team_type.lower() in ("individual", "free", "")
        app_id_limit = 10 if is_free else 100

        return {
            "team": {
                "name": team.get("name", ""),
                "team_id": team_id,
                "type": team_type,
                "is_free": is_free,
            },
            "apps": apps,
            "app_count": len(app_ids),
            "app_limit": app_id_limit,
            "auto_refresh_window_hours": _refresh.REFRESH_WINDOW_HOURS,
            "apple_id": session.apple_id,
            "sync": sync_result,
        }
    except Exception as e:
        logger.exception("Account info fetch failed")
        return JSONResponse({"error": str(e)}, status_code=500)


async def _install_record_for_identifier(identifier: str, team_id: str) -> tuple[dict | None, bool]:
    """Find the saved install record that owns an App ID identifier."""
    if not identifier:
        return None, False

    state = _refresh.load_state()
    records = sorted(
        state.get("installs", []),
        key=lambda item: item.get("last_installed", 0),
        reverse=True,
    )
    for rec in records:
        resolved_ipa = _vault.resolve_ipa_path(rec)
        direct_ids = {
            rec.get("bundle_id", ""),
            rec.get("source_bundle_id", ""),
        }
        if rec.get("source_bundle_id"):
            direct_ids.add(dev_services.sideload_bundle_id(team_id, rec["source_bundle_id"]))
        if identifier in direct_ids:
            return rec, False

        if resolved_ipa is None:
            continue
        try:
            ipa_info = await ipa_processor.inspect(resolved_ipa)
            original_id = ipa_info["bundle_id"]
            sideload_id = dev_services.sideload_bundle_id(team_id, original_id)
            parent_ids = {
                original_id,
                sideload_id,
                rec.get("bundle_id", ""),
                rec.get("source_bundle_id", ""),
            }
            if identifier in parent_ids:
                return rec, False

            for extension in await ipa_processor.inspect_extensions(resolved_ipa):
                extension_id = extension.get("bundle_id", "")
                if not extension_id:
                    continue
                for parent_id in parent_ids:
                    if not parent_id:
                        continue
                    target_extension_id = (
                        extension_id
                        if parent_id == original_id
                        else _rewrite_nested_bundle_id(original_id, parent_id, extension_id)
                    )
                    if identifier == target_extension_id:
                        return rec, True
        except Exception:
            logger.debug("Could not inspect saved install record %s", rec.get("ipa_path", ""), exc_info=True)
            continue
    return None, False


@app.post("/api/account/reinstall-app")
async def reinstall_app(payload: dict):
    """Re-sign and reinstall an app from its saved install record."""
    session = auth_client.session
    if not session or not session.authenticated:
        return JSONResponse({"status": "error", "message": "Not authenticated"}, status_code=401)

    app_id_id = payload.get("app_id_id", "")
    identifier = payload.get("identifier", "")
    job = job_manager.start(
        "reinstall",
        "Reinstall app",
        target=identifier or app_id_id,
        message="Checking saved install...",
    )

    try:
        team = await dev_services.get_team(session)
        team_id = team["teamId"]

        if app_id_id and not identifier:
            for app_id in await dev_services._list_app_ids(session, team_id):
                if app_id.get("appIdId") == app_id_id:
                    identifier = app_id.get("identifier", "")
                    break

        if not identifier:
            message = "App ID identifier is required"
            job_manager.fail(job, message)
            return JSONResponse(
                _annotate_job(_error_payload(message), job),
                status_code=400,
            )

        rec, is_extension = await _install_record_for_identifier(identifier, team_id)
        if not rec:
            message = "No saved install was found for this App ID. Install it once from an IPA first."
            job_manager.fail(job, message)
            return JSONResponse(
                _annotate_job(_error_payload(message), job),
                status_code=404,
            )

        ipa_path = rec.get("ipa_path", "")
        resolved_ipa = _vault.resolve_ipa_path(rec)
        if resolved_ipa is None:
            message = "The saved IPA file for this app is missing. Choose the IPA again, then install."
            job_manager.fail(job, message)
            return JSONResponse(
                _annotate_job(_error_payload(message), job),
                status_code=404,
            )

        job_manager.update(job, progress=5, message="Found saved IPA. Reinstalling...")
        result = await _install_app(
            rec["device_udid"],
            str(resolved_ipa),
            lambda step, progress, message: _job_progress(job, _noop_progress, step, progress, message),
            device_name_hint=rec.get("device_name", ""),
        )
        if is_extension:
            result["message"] = "Reinstalled the parent app and refreshed its extension."
        else:
            result["message"] = "Reinstalled successfully."
        job_manager.complete(job, message=result["message"])
        result["job_id"] = job.id
        return result
    except Exception as e:
        logger.exception("Manual reinstall failed")
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job(_error_payload(e), job), status_code=500)


@app.post("/api/account/delete-app")
async def delete_app_id(payload: dict):
    """Delete a registered app ID from Apple Developer Services."""
    session = auth_client.session
    if not session or not session.authenticated:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    app_id_id = payload.get("app_id_id", "")
    if not app_id_id:
        return JSONResponse({"error": "app_id_id is required"}, status_code=400)

    try:
        team = await dev_services.get_team(session)
        team_id = team["teamId"]

        # Delete any provisioning profiles for this app first
        await dev_services._delete_profiles_for_app(session, team_id, app_id_id)

        # Delete the app ID
        await dev_services._request(
            session,
            "ios/deleteAppId.action",
            {"teamId": team_id, "appIdId": app_id_id},
        )
        logger.info("Deleted app ID: %s", app_id_id)
        return {"status": "ok"}
    except Exception as e:
        logger.exception("Failed to delete app ID %s", app_id_id)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/upload")
async def upload_ipa(file: UploadFile):
    job = job_manager.start(
        "upload",
        "Upload IPA",
        target=file.filename or "",
        message="Validating upload...",
    )
    if not file.filename or not file.filename.endswith(".ipa"):
        message = "Only .ipa files are accepted"
        job_manager.fail(job, message)
        return JSONResponse(_annotate_job({"error": message, **_error_payload(message)}, job), status_code=400)
    try:
        logger.info("Receiving IPA upload: %s", file.filename)
        job_manager.update(job, progress=20, message="Saving IPA...")
        ipa_path = await ipa_processor.save_upload(file)
        job_manager.update(job, progress=80, message="Inspecting IPA...")
        info = await ipa_processor.inspect(ipa_path)
        vaulted = _vault.store_ipa(ipa_path, original_filename=file.filename)
        ipa_path = Path(vaulted["path"])
        info["vault"] = vaulted
        logger.info("IPA upload ready: %s (%s)", info.get("bundle_name") or file.filename, info.get("bundle_id"))
        job_manager.complete(job, message="IPA upload ready.")
    except ValueError as e:
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job({"error": str(e), **_error_payload(e)}, job), status_code=400)
    except Exception as e:
        logger.exception("IPA upload failed")
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job({"error": str(e), **_error_payload(e)}, job), status_code=500)
    return {"path": str(ipa_path), "info": info, "job_id": job.id}


@app.post("/api/upload/raw")
async def upload_ipa_raw(request: Request):
    filename = request.headers.get("x-catapult-filename", "upload.ipa")
    job = job_manager.start(
        "upload",
        "Upload IPA",
        target=filename,
        message="Validating upload...",
    )
    if not filename.lower().endswith(".ipa"):
        message = "Only .ipa files are accepted"
        job_manager.fail(job, message)
        return JSONResponse(_annotate_job({"error": message, **_error_payload(message)}, job), status_code=400)
    try:
        logger.info("Receiving raw IPA upload: %s", filename)
        job_manager.update(job, progress=20, message="Saving IPA...")
        ipa_path = await ipa_processor.save_raw_upload(request.stream())
        job_manager.update(job, progress=80, message="Inspecting IPA...")
        info = await ipa_processor.inspect(ipa_path)
        vaulted = _vault.store_ipa(ipa_path, original_filename=filename)
        ipa_path = Path(vaulted["path"])
        info["vault"] = vaulted
        logger.info("IPA upload ready: %s (%s)", info.get("bundle_name") or filename, info.get("bundle_id"))
        job_manager.complete(job, message="IPA upload ready.")
    except ValueError as e:
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job({"error": str(e), **_error_payload(e)}, job), status_code=400)
    except Exception as e:
        logger.exception("IPA upload failed")
        job_manager.fail(job, e)
        return JSONResponse(_annotate_job({"error": str(e), **_error_payload(e)}, job), status_code=500)
    return {"path": str(ipa_path), "info": info, "job_id": job.id}


# ── WebSocket install flow ──


async def _send(ws: WebSocket, step: str, progress: int, message: str, **extra):
    payload = {"step": step, "progress": progress, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    await ws.send_json(payload)


async def _send_error(ws: WebSocket, error: BaseException | str, *, job: ActivityJob | None = None, progress: int = 0):
    normalized = normalize_error(error)
    await _send(
        ws,
        "error",
        progress,
        normalized.message,
        job_id=job.id if job else None,
        category=normalized.category,
        detail=redact_sensitive(normalized.detail),
    )


async def _noop_progress(step: str, progress: int, message: str):
    logger.info("Install progress: %s %d%% %s", step, progress, message)


async def _job_progress(job: ActivityJob, downstream, step: str, progress: int, message: str):
    job_manager.update(job, step=step, progress=progress, message=message)
    await downstream(step, progress, message)


def _rewrite_nested_bundle_id(parent_old_id: str, parent_new_id: str, nested_old_id: str) -> str:
    if nested_old_id == parent_old_id:
        return parent_new_id
    if nested_old_id.startswith(parent_old_id + "."):
        return parent_new_id + nested_old_id[len(parent_old_id):]
    return f"{parent_new_id}.{nested_old_id.rsplit('.', 1)[-1]}"


async def _install_app(
    device_udid: str,
    ipa_path: str,
    progress,
    device_name_hint: str = "",
):
    session = auth_client.session

    if not ipa_path:
        raise RuntimeError("Install request was missing the IPA file.")

    ipa_file = Path(ipa_path).expanduser()
    if not ipa_file.exists():
        raise RuntimeError(f"IPA file is missing: {ipa_file}. Choose the IPA again before installing.")
    vaulted = _vault.store_ipa(ipa_file, original_filename=ipa_file.name)
    ipa_file = Path(vaulted["path"])

    await progress("preflight", 0, "Checking IPA...")
    ipa_info = await ipa_processor.inspect(str(ipa_file))

    if not session or not session.authenticated:
        raise RuntimeError("Not authenticated. Please sign in first.")

    # 1. Resolve and verify the target device before touching Apple account state.
    await progress("preflight", 5, "Checking device...")
    try:
        device_info = await device_manager.get_device_info(device_udid)
    except Exception as e:
        target = device_name_hint or device_udid
        raise RuntimeError(
            f"{target} is not connected or reachable. For iPhone/iPad, connect USB, unlock it, "
            "and trust this Mac. For Apple TV, keep it on the same network and connect/setup "
            f"the tunnel. Details: {e}"
        ) from e
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

    # 2. Team + certificate
    await progress("signing", 10, "Fetching team...")
    team = await dev_services.get_team(session)
    team_id = team["teamId"]

    await progress("signing", 20, "Preparing signing certificate...")
    cert, private_key = await dev_services.get_or_create_cert(session, team_id)

    # 3. Register device — get real UDID from RSD, not mDNS name or pairing UUID
    await progress("signing", 30, "Registering device...")
    await dev_services.register_device(session, team_id, real_udid, device_info["name"])

    # 4. App ID + provisioning profile
    await progress("signing", 35, "Checking installed apps...")
    original_bundle_id = ipa_info["bundle_id"]
    legacy_bundle_id = dev_services.sideload_bundle_id(team_id, original_bundle_id)
    installed_app = await device_manager.find_installed_app(
        bundle_id=original_bundle_id,
        display_name=ipa_info.get("bundle_name", ""),
        candidate_bundle_ids=[legacy_bundle_id],
        team_id=team_id,
        device_udid=device_udid,
    )
    recorded_bundle_id = _refresh.find_recorded_bundle_id(
        device_udid,
        [original_bundle_id, legacy_bundle_id],
    )
    target_bundle_id = (
        installed_app.get("bundle_id")
        if installed_app
        else recorded_bundle_id
        or legacy_bundle_id
    )
    if installed_app and target_bundle_id != original_bundle_id:
        await progress(
            "signing",
            40,
            f"Updating installed {installed_app.get('name') or ipa_info.get('bundle_name') or 'app'} copy...",
        )
    elif recorded_bundle_id and target_bundle_id != original_bundle_id:
        await progress("signing", 40, "Updating recorded installed copy...")
    else:
        await progress("signing", 40, "Registering app ID...")
    app_id = await dev_services.register_app_id(session, team_id, target_bundle_id)

    await progress("signing", 50, "Creating provisioning profile...")
    profile = await dev_services.create_profile(
        session, team_id, app_id, cert, real_udid, sub_platform=sub_platform
    )

    rewrite_bundle_id = target_bundle_id if target_bundle_id != original_bundle_id else None
    extension_profiles: dict[str, bytes] = {}
    extension_infos = await ipa_processor.inspect_extensions(str(ipa_file))
    if extension_infos and rewrite_bundle_id:
        for extension in extension_infos:
            extension_bundle_id = extension.get("bundle_id", "")
            if not extension_bundle_id:
                continue
            target_extension_bundle_id = _rewrite_nested_bundle_id(
                original_bundle_id,
                target_bundle_id,
                extension_bundle_id,
            )
            await progress("signing", 55, f"Provisioning {extension.get('bundle_name') or 'app extension'}...")
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

    # 5. Sign while preserving the IPA bundle ID so installs update in place.
    await progress("signing", 60, "Signing IPA...")
    signed_path = await signer.sign(
        str(ipa_file),
        cert,
        private_key,
        profile,
        rewrite_bundle_id,
        extension_profiles=extension_profiles,
    )

    # 6. Install
    await progress("installing", 80, f"Installing to {device_info['name']}...")
    await device_manager.install(device_udid, signed_path)

    logger.info(
        "Install complete: %s signed as %s → %s",
        ipa_info["bundle_id"],
        target_bundle_id,
        device_info["name"],
    )
    _refresh.record_install(
        device_udid,
        str(ipa_file),
        device_info["name"],
        bundle_id=target_bundle_id,
        source_bundle_id=original_bundle_id,
        app_name=ipa_info.get("bundle_name", ""),
        ipa_sha256=vaulted["sha256"],
        ipa_size=vaulted["size"],
        original_filename=vaulted["original_filename"],
    )
    await _sync_state_for_team(session.apple_id, team_id)

    return {
        "status": "ok",
        "message": "Installed successfully.",
        "device_name": device_info["name"],
        "bundle_id": target_bundle_id,
        "source_bundle_id": original_bundle_id,
        "app_name": ipa_info.get("bundle_name", ""),
        "ipa_sha256": vaulted["sha256"],
    }


@app.websocket("/ws/reinstall")
async def reinstall_ws(ws: WebSocket):
    await ws.accept()
    job: ActivityJob | None = None
    try:
        message = await ws.receive()
        if message.get("text") is not None:
            params = json.loads(message["text"])
        elif message.get("bytes") is not None:
            params = json.loads(message["bytes"].decode("utf-8"))
        else:
            await _send_error(ws, "Reinstall request was empty.")
            return

        app_id_id = params.get("app_id_id", "")
        identifier = params.get("identifier", "")
        job = job_manager.start(
            "reinstall",
            "Reinstall app",
            target=identifier or app_id_id,
            message="Checking saved install...",
        )

        session = auth_client.session
        if not session or not session.authenticated:
            message = "Not authenticated. Please sign in first."
            job_manager.fail(job, message)
            await _send_error(ws, message, job=job)
            return

        await _send(ws, "preflight", 0, "Checking saved install...", job_id=job.id)

        team = await dev_services.get_team(session)
        team_id = team["teamId"]

        if app_id_id and not identifier:
            for app_id in await dev_services._list_app_ids(session, team_id):
                if app_id.get("appIdId") == app_id_id:
                    identifier = app_id.get("identifier", "")
                    break

        if not identifier:
            message = "App ID identifier is missing."
            job_manager.fail(job, message)
            await _send_error(ws, message, job=job)
            return

        rec, is_extension = await _install_record_for_identifier(identifier, team_id)
        if not rec:
            message = "No saved install was found. Install from an IPA first."
            job_manager.fail(job, message)
            await _send_error(ws, message, job=job)
            return

        resolved_ipa = _vault.resolve_ipa_path(rec)
        if resolved_ipa is None:
            message = "Saved IPA is missing. Choose the IPA again before reinstalling."
            job_manager.fail(job, message)
            await _send_error(ws, message, job=job)
            return

        device_name = rec.get("device_name", "") or "saved device"
        job_manager.update(job, progress=5, message=f"Found saved IPA. Reinstalling to {device_name}...")
        await _send(ws, "preflight", 5, f"Found saved IPA. Reinstalling to {device_name}...", job_id=job.id)

        result = await _install_app(
            rec["device_udid"],
            str(resolved_ipa),
            lambda step, progress, message: _job_progress(
                job,
                lambda s, p, m: _send(ws, s, p, m, job_id=job.id),
                step,
                progress,
                message,
            ),
            device_name_hint=device_name,
        )
        message = (
            "Reinstalled parent app and refreshed extension."
            if is_extension
            else result.get("message") or "Reinstalled successfully."
        )
        job_manager.complete(job, message=message)
        await _send(ws, "done", 100, message, job_id=job.id)
    except Exception as e:
        logger.exception("Reinstall failed")
        if job:
            job_manager.fail(job, e)
        try:
            await _send_error(ws, e, job=job)
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/ws/install")
async def install_ws(ws: WebSocket):
    await ws.accept()
    job: ActivityJob | None = None
    try:
        message = await ws.receive()
        if message.get("text") is not None:
            params = json.loads(message["text"])
        elif message.get("bytes") is not None:
            params = json.loads(message["bytes"].decode("utf-8"))
        else:
            await _send_error(ws, "Install request was empty.")
            return

        device_udid = params.get("device_udid")
        ipa_path = params.get("ipa_path")
        job = job_manager.start(
            "install",
            "Install IPA",
            target=device_udid or "",
            message="Checking install request...",
        )
        if not device_udid or not ipa_path:
            message = "Install request was missing the device or app."
            job_manager.fail(job, message)
            await _send_error(ws, message, job=job)
            return

        result = await _install_app(
            device_udid,
            ipa_path,
            lambda step, progress, message: _job_progress(
                job,
                lambda s, p, m: _send(ws, s, p, m, job_id=job.id),
                step,
                progress,
                message,
            ),
        )
        done_message = result.get("message") or "Installed successfully!"
        job_manager.complete(job, message=done_message)
        await _send(ws, "done", 100, done_message, job_id=job.id)

    except Exception as e:
        logger.exception("Install failed")
        if job:
            job_manager.fail(job, e)
        try:
            await _send_error(ws, e, job=job)
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
