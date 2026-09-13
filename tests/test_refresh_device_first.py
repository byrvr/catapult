"""_refresh_install() reaches the device before it touches Apple.

A refresh used to fetch the team, mint or reuse a certificate, register the
device and issue a profile, and only then discover that the Apple TV was off
or on another network. Retried hourly with backoff, that is how a paid team
ended up with dozens of development certificates and an inbox full of "Your
Certificate Has Been Revoked" mail. Nothing may reach Apple until the device
has answered.
"""

from catapult import refresh


class _Session:
    authenticated = True


class _Auth:
    session = _Session()


class _DevServices:
    """Records every Apple-facing call; none of them is expected."""

    def __init__(self):
        self.calls: list[str] = []

    async def get_team(self, session):
        self.calls.append("get_team")
        return {"teamId": "TEAM"}

    async def get_or_create_cert(self, session, team_id, *, personal_team=True):
        self.calls.append("get_or_create_cert")
        return b"cert", object()

    async def register_device(self, *args, **kwargs):
        self.calls.append("register_device")

    async def register_app_id(self, *args, **kwargs):
        self.calls.append("register_app_id")
        return {}

    async def create_profile(self, *args, **kwargs):
        self.calls.append("create_profile")
        return b"profile"


class _MissingDevice:
    async def get_device_info(self, udid):
        raise RuntimeError(f"Device {udid} not found on the network")


class _TunnelDown:
    async def get_device_info(self, udid):
        return {"udid": udid, "service": "_remotepairing._tcp.local.", "host": "10.0.0.5"}

    async def start_tunnel(self, **kwargs):
        return {"status": "error", "message": "Apple TV tunnel is not ready."}


def _record(tmp_path, monkeypatch) -> dict:
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"ipa")
    monkeypatch.setattr(refresh, "STATE_DIR", tmp_path)
    monkeypatch.setattr(refresh, "STATE_FILE", tmp_path / "state.json")
    rec = {"device_udid": "TV1", "ipa_path": str(ipa), "app_name": "App", "fail_count": 0}
    refresh.save_state({"installs": [dict(rec)]})
    return rec


async def test_an_unreachable_device_costs_no_apple_calls(tmp_path, monkeypatch):
    rec = _record(tmp_path, monkeypatch)
    apple = _DevServices()

    result = await refresh._refresh_install(rec, _MissingDevice(), _Auth(), apple, object(), object())

    assert result["status"] == "error"
    assert "not found" in result["message"]
    assert apple.calls == []
    assert rec["fail_count"] == 1
    assert rec["next_attempt_at"] > 0


async def test_a_tunnel_that_will_not_come_up_costs_no_apple_calls(tmp_path, monkeypatch):
    rec = _record(tmp_path, monkeypatch)
    apple = _DevServices()

    result = await refresh._refresh_install(rec, _TunnelDown(), _Auth(), apple, object(), object())

    assert result["status"] == "error"
    assert "tunnel" in result["message"].lower()
    assert apple.calls == []
