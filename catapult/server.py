"""FastAPI server — REST + WebSocket API for the Catapult UI."""

import asyncio
import hashlib
import logging
import sys
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from catapult.apple_auth import AppleAuthClient
from catapult.developer import DeveloperServices
from catapult.device import DeviceManager
from catapult.ipa import IpaProcessor
from catapult.signer import Signer
from catapult import refresh as _refresh

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


@app.on_event("startup")
async def _on_startup():
    # Restore saved session on startup
    _refresh.restore_session(auth_client)
    # Start 7-day auto-refresh background loop
    def _components():
        return device_manager, auth_client, dev_services, signer, ipa_processor
    asyncio.create_task(_refresh.run_refresh_loop(_components))


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


@app.get("/api/devices")
async def list_devices():
    try:
        devices = await device_manager.discover()
        return {"devices": devices}
    except Exception as e:
        logger.exception("Device scan failed")
        return JSONResponse({"devices": [], "error": str(e)}, status_code=500)


@app.post("/api/devices/pair")
async def pair_device(payload: dict = None):
    """Initiate device pairing — shows admin password dialog and PIN on device."""
    name = payload.get("name") if payload else None
    return await device_manager.pair_device(device_name=name)


@app.post("/api/devices/tunnel")
async def start_tunnel():
    """Start a tunnel to paired devices — shows admin password dialog."""
    result = await device_manager.start_tunnel()
    if result.get("status") == "ok":
        # Rescan to find newly available devices
        await device_manager.discover()
    return result


@app.post("/api/devices/setup")
async def setup_device(payload: dict = None):
    """Pair + start tunnel for a device."""
    name = payload.get("name") if payload else None
    pair_result = await device_manager.pair_device(device_name=name)
    if pair_result.get("status") != "ok":
        return pair_result
    # After successful pairing, start tunnel
    tunnel_result = await device_manager.start_tunnel()
    if tunnel_result.get("status") == "ok":
        await asyncio.sleep(2)
        await device_manager.discover()
        # Mark all remotepairing devices as installable now that tunnel is up
        for d in device_manager._cache.values():
            if d.get("needs_setup"):
                d["installable"] = True
                d["needs_setup"] = False
                device_manager._tunneled_hosts.add(d["host"])
    return tunnel_result


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
    result = await auth_client.authenticate(apple_id, password)
    if result.get("status") == "ok":
        return await _fetch_team(result)
    return result


@app.post("/api/auth/2fa")
async def verify_2fa(payload: dict):
    code = payload.get("code", "")
    if not code:
        return JSONResponse({"status": "error", "message": "Code is required"}, status_code=400)
    result = await auth_client.submit_2fa(code)
    if result.get("status") == "ok":
        return await _fetch_team(result)
    return result


async def _fetch_team(auth_result: dict) -> dict:
    """Fetch development team right after successful auth."""
    try:
        session = auth_client.session
        team = await dev_services.get_team(session)
        logger.info("Team: %s (%s)", team.get("name"), team.get("teamId"))
        _refresh.save_session(session)
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

        app_ids = await dev_services._list_app_ids(session, team_id)

        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)

        # Load install history and map by sideload bundle identifier
        install_state = _refresh.load_state()
        install_records = install_state.get("installs", [])
        install_by_bundle = {}
        for r in install_records:
            path = r.get("ipa_path", "")
            try:
                info = await ipa_processor.inspect(path)
                sideload_id = dev_services.sideload_bundle_id(team_id, info["bundle_id"])
                install_by_bundle[sideload_id] = r
            except Exception:
                pass

        # Format app IDs with computed expiry and install dates
        apps = []
        for a in app_ids:
            identifier = a.get("identifier", "")
            name = a.get("name", "")

            # Find install record for this app
            rec = install_by_bundle.get(identifier)
            installed_str = None
            installed_device = None
            days_left = None
            exp_str = None

            if rec:
                ts = rec.get("last_installed")
                if ts:
                    installed_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                    installed_str = installed_dt.strftime("%b %d, %Y %H:%M")
                    # Free accounts: profile expires 7 days after signing
                    expiry_dt = installed_dt + timedelta(days=7)
                    delta = expiry_dt - now
                    days_left = max(0, delta.days)
                    exp_str = expiry_dt.strftime("%b %d, %Y")
                installed_device = rec.get("device_name", "")

            apps.append({
                "name": name,
                "identifier": identifier,
                "expiry": exp_str,
                "days_left": days_left,
                "installed": installed_str,
                "installed_device": installed_device,
            })

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
            "apple_id": session.apple_id,
        }
    except Exception as e:
        logger.exception("Account info fetch failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/upload")
async def upload_ipa(file: UploadFile):
    if not file.filename or not file.filename.endswith(".ipa"):
        return JSONResponse({"error": "Only .ipa files are accepted"}, status_code=400)
    try:
        ipa_path = await ipa_processor.save_upload(file)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    info = await ipa_processor.inspect(ipa_path)
    return {"path": str(ipa_path), "info": info}


# ── WebSocket install flow ──


async def _send(ws: WebSocket, step: str, progress: int, message: str):
    await ws.send_json({"step": step, "progress": progress, "message": message})


@app.websocket("/ws/install")
async def install_ws(ws: WebSocket):
    await ws.accept()
    try:
        params = await ws.receive_json()
        device_udid = params["device_udid"]
        ipa_path = params["ipa_path"]
        session = auth_client.session

        if not session or not session.authenticated:
            await _send(ws, "error", 0, "Not authenticated. Please sign in first.")
            return

        # 1. Team + certificate
        await _send(ws, "signing", 0, "Fetching team...")
        team = await dev_services.get_team(session)
        team_id = team["teamId"]

        await _send(ws, "signing", 10, "Preparing signing certificate...")
        cert, private_key = await dev_services.get_or_create_cert(session, team_id)

        # 2. Register device — get real UDID from RSD, not mDNS name or pairing UUID
        await _send(ws, "signing", 25, "Registering device...")
        device_info = await device_manager.get_device_info(device_udid)
        real_udid, sub_platform = await device_manager.get_real_udid()
        await dev_services.register_device(session, team_id, real_udid, device_info["name"])

        # 3. App ID + provisioning profile
        await _send(ws, "signing", 40, "Registering app ID...")
        ipa_info = await ipa_processor.inspect(ipa_path)
        original_bundle_id = ipa_info["bundle_id"]
        sideload_bundle_id = dev_services.sideload_bundle_id(team_id, original_bundle_id)
        app_id = await dev_services.register_app_id(session, team_id, original_bundle_id)

        await _send(ws, "signing", 50, "Creating provisioning profile...")
        profile = await dev_services.create_profile(
            session, team_id, app_id, cert, real_udid, sub_platform=sub_platform
        )

        # 4. Sign (with modified bundle ID for sideloading)
        await _send(ws, "signing", 60, "Signing IPA...")
        signed_path = await signer.sign(ipa_path, cert, private_key, profile, sideload_bundle_id)

        # 5. Install
        await _send(ws, "installing", 80, f"Installing to {device_info['name']}...")
        await device_manager.install(device_udid, signed_path)

        await _send(ws, "done", 100, "Installed successfully!")
        logger.info("Install complete: %s → %s", ipa_info["bundle_id"], device_info["name"])
        _refresh.record_install(device_udid, ipa_path, device_info["name"])

    except Exception as e:
        logger.exception("Install failed")
        try:
            await _send(ws, "error", 0, str(e))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
