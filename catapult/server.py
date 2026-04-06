from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from catapult.device import DeviceManager
from catapult.apple_auth import AppleAuthClient
from catapult.developer import DeveloperServices
from catapult.signer import Signer
from catapult.ipa import IpaProcessor

app = FastAPI(title="Catapult")

static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

device_manager = DeviceManager()
auth_client = AppleAuthClient()
dev_services = DeveloperServices()
signer = Signer()
ipa_processor = IpaProcessor()


@app.get("/")
async def index():
    return FileResponse(static_dir / "index.html")


@app.get("/api/devices")
async def list_devices():
    devices = await device_manager.discover()
    return {"devices": devices}


@app.post("/api/auth/login")
async def login(payload: dict):
    apple_id = payload["apple_id"]
    password = payload["password"]
    result = await auth_client.authenticate(apple_id, password)
    return result


@app.post("/api/auth/2fa")
async def verify_2fa(payload: dict):
    code = payload["code"]
    result = await auth_client.submit_2fa(code)
    return result


@app.post("/api/upload")
async def upload_ipa(file: UploadFile):
    ipa_path = await ipa_processor.save_upload(file)
    info = await ipa_processor.inspect(ipa_path)
    return {"path": str(ipa_path), "info": info}


@app.websocket("/ws/install")
async def install_ws(ws: WebSocket):
    await ws.accept()
    try:
        params = await ws.receive_json()

        device_udid = params["device_udid"]
        ipa_path = params["ipa_path"]

        await ws.send_json({"step": "signing", "progress": 0, "message": "Preparing signing certificate..."})
        session = auth_client.session
        team = await dev_services.get_team(session)
        cert, private_key = await dev_services.get_or_create_cert(session, team["teamId"])

        await ws.send_json({"step": "signing", "progress": 20, "message": "Registering device..."})
        device_info = await device_manager.get_device_info(device_udid)
        await dev_services.register_device(session, team["teamId"], device_udid, device_info["name"])

        await ws.send_json({"step": "signing", "progress": 40, "message": "Creating provisioning profile..."})
        ipa_info = await ipa_processor.inspect(ipa_path)
        bundle_id = ipa_info["bundle_id"]
        app_id = await dev_services.register_app_id(session, team["teamId"], bundle_id)
        profile = await dev_services.create_profile(session, team["teamId"], app_id, cert, device_udid)

        await ws.send_json({"step": "signing", "progress": 60, "message": "Signing IPA..."})
        signed_path = await signer.sign(ipa_path, cert, private_key, profile)

        await ws.send_json({"step": "installing", "progress": 80, "message": "Installing to device..."})
        await device_manager.install(device_udid, signed_path)

        await ws.send_json({"step": "done", "progress": 100, "message": "Installed successfully!"})
    except Exception as e:
        await ws.send_json({"step": "error", "progress": 0, "message": str(e)})
    finally:
        await ws.close()
