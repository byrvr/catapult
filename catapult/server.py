"""FastAPI server — REST + WebSocket API for the Catapult UI."""

import logging
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from catapult.apple_auth import AppleAuthClient
from catapult.developer import DeveloperServices
from catapult.device import DeviceManager
from catapult.ipa import IpaProcessor
from catapult.signer import Signer

logger = logging.getLogger(__name__)

app = FastAPI(title="Catapult")

static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

device_manager = DeviceManager()
auth_client = AppleAuthClient()
dev_services = DeveloperServices()
signer = Signer()
ipa_processor = IpaProcessor()


# ── Pages ──


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


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
async def pair_device():
    """Initiate device pairing — shows admin password dialog and PIN on device."""
    return await device_manager.pair_device()


@app.post("/api/devices/tunnel")
async def start_tunnel():
    """Start a tunnel to paired devices — shows admin password dialog."""
    result = await device_manager.start_tunnel()
    if result.get("status") == "ok":
        # Rescan to find newly available devices
        await device_manager.discover()
    return result


@app.post("/api/devices/setup")
async def setup_device():
    """One-click pair + tunnel for unpaired devices."""
    pair_result = await device_manager.pair_device()
    if pair_result.get("status") != "ok":
        return pair_result
    tunnel_result = await device_manager.start_tunnel()
    if tunnel_result.get("status") == "ok":
        await device_manager.discover()
    return tunnel_result


@app.get("/api/auth/status")
async def auth_status():
    """Check if there's an active authenticated session."""
    if auth_client.session and auth_client.session.authenticated:
        return {"authenticated": True, "apple_id": auth_client.session.apple_id}
    return {"authenticated": False}


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
        return auth_result
    except Exception as e:
        logger.error("Team fetch failed: %s", e)
        return {"status": "error", "message": f"Signed in but team fetch failed: {e}"}


@app.post("/api/upload")
async def upload_ipa(file: UploadFile):
    if not file.filename or not file.filename.endswith(".ipa"):
        return JSONResponse({"error": "Only .ipa files are accepted"}, status_code=400)
    ipa_path = await ipa_processor.save_upload(file)
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

        # 2. Register device
        await _send(ws, "signing", 25, "Registering device...")
        device_info = await device_manager.get_device_info(device_udid)
        await dev_services.register_device(session, team_id, device_udid, device_info["name"])

        # 3. App ID + provisioning profile
        await _send(ws, "signing", 40, "Registering app ID...")
        ipa_info = await ipa_processor.inspect(ipa_path)
        original_bundle_id = ipa_info["bundle_id"]
        sideload_bundle_id = dev_services.sideload_bundle_id(team_id, original_bundle_id)
        app_id = await dev_services.register_app_id(session, team_id, original_bundle_id)

        await _send(ws, "signing", 50, "Creating provisioning profile...")
        profile = await dev_services.create_profile(session, team_id, app_id, cert, device_udid)

        # 4. Sign (with modified bundle ID for sideloading)
        await _send(ws, "signing", 60, "Signing IPA...")
        signed_path = await signer.sign(ipa_path, cert, private_key, profile, sideload_bundle_id)

        # 5. Install
        await _send(ws, "installing", 80, f"Installing to {device_info['name']}...")
        await device_manager.install(device_udid, signed_path)

        await _send(ws, "done", 100, "Installed successfully!")
        logger.info("Install complete: %s → %s", ipa_info["bundle_id"], device_info["name"])

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
