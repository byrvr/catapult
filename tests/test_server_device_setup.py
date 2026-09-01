"""POST /api/devices/setup on a USB device stops after the Trust step.

Setup was written for Apple TV: pair, then start a tunnel. A USB iPhone or iPad
pairs by trusting the Mac and has no tunnel to start — running the tunnel step
would poll for minutes and can prompt for an admin password.
"""

import pytest
from fastapi.testclient import TestClient

from catapult import server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server.auth_client, "session", None, raising=False)
    return TestClient(server.app)


def test_usb_setup_does_not_start_a_tunnel(client, monkeypatch):
    usb = {"udid": "USB1", "name": "Tablet", "host": "usb:USB1", "service": "usbmux",
           "device_class": "ipados", "installable": False, "needs_setup": True}
    calls = []

    monkeypatch.setattr(server.device_manager, "_selected_device", lambda **kw: usb)
    monkeypatch.setattr(server.device_manager, "_is_known_paired", lambda device: False)

    async def pair_device(**kwargs):
        calls.append("pair")
        return {"status": "ok", "message": "Trusted. The iPad is ready to install."}

    async def start_tunnel(**kwargs):
        calls.append("tunnel")
        return {"status": "error", "message": "no tunnel for a USB device"}

    async def discover(*args, **kwargs):
        calls.append("discover")
        return []

    monkeypatch.setattr(server.device_manager, "pair_device", pair_device)
    monkeypatch.setattr(server.device_manager, "start_tunnel", start_tunnel)
    monkeypatch.setattr(server.device_manager, "discover", discover)

    response = client.post("/api/devices/setup", json={"udid": "USB1", "name": "Tablet"})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "tunnel" not in calls
    assert calls[0] == "pair"
