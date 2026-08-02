"""Admin-password escalation for the tunneld LaunchDaemon.

The daemon is installed once and survives reboots, app rebuilds, and the ad-hoc
signature. Every prompt after that first one came from Catapult tearing down its
own working daemon:

  - "wedged" was gated on `uptime >= 3600`, which is the normal steady state, so
    an Apple TV that was merely asleep triggered a root reinstall
  - the privileged script begins with `bootout` + `pkill -9`, so each prompt
    destroyed the daemon and guaranteed the next one
  - it fired from the hourly background refresh, with nobody there to answer
  - a new random temp script per call defeats osascript's 5-minute credential
    cache, so even back-to-back escalations both prompt
"""

import pytest

from catapult.device import DeviceManager, TUNNEL_FAILURE_CACHE_SECONDS


@pytest.fixture
def manager():
    return DeviceManager()


async def test_background_callers_cannot_trigger_an_admin_prompt(manager, monkeypatch):
    """The hourly refresh must never pop a password dialog."""
    escalated = False

    async def no_daemon():
        return False

    async def install():
        nonlocal escalated
        escalated = True
        return {"status": "ok"}

    monkeypatch.setattr(manager, "_ensure_tunneld", no_daemon)
    monkeypatch.setattr(manager, "_start_tunneld", install)

    result = await manager.start_tunnel(device_udid="ATV", allow_escalation=False)

    assert not escalated
    assert result["status"] == "error"
    assert "Setup" in result["message"]


async def test_foreground_callers_may_still_escalate(manager, monkeypatch):
    """A user pressing Setup is exactly when asking is appropriate."""
    escalated = False

    async def no_daemon():
        return False

    async def install():
        nonlocal escalated
        escalated = True
        return {"status": "error", "message": "declined"}

    monkeypatch.setattr(manager, "_ensure_tunneld", no_daemon)
    monkeypatch.setattr(manager, "_start_tunneld", install)

    await manager.start_tunnel(device_udid="ATV", allow_escalation=True)

    assert escalated


async def test_repeated_failures_are_cached(manager, monkeypatch):
    """One install calls start_tunnel up to four times; an unreachable device
    should not produce four full poll cycles."""
    calls = 0

    async def failing(**kwargs):
        nonlocal calls
        calls += 1
        return {"status": "error", "message": "no tunnel"}

    monkeypatch.setattr(manager, "_start_tunnel_inner", failing)

    for _ in range(4):
        await manager.start_tunnel(device_udid="ATV")

    assert calls == 1


async def test_success_clears_the_failure_cache(manager, monkeypatch):
    results = [
        {"status": "error", "message": "no tunnel"},
        {"status": "ok", "message": "up"},
    ]

    async def flaky(**kwargs):
        return results.pop(0) if results else {"status": "ok", "message": "up"}

    monkeypatch.setattr(manager, "_start_tunnel_inner", flaky)

    assert (await manager.start_tunnel(device_udid="ATV"))["status"] == "error"
    manager._recent_tunnel_failures.clear()          # simulate the window elapsing
    assert (await manager.start_tunnel(device_udid="ATV"))["status"] == "ok"
    # A success must not leave a stale failure behind.
    assert "ATV" not in manager._recent_tunnel_failures


async def test_failures_are_cached_per_device(manager, monkeypatch):
    calls: list[str] = []

    async def failing(device_udid=None, **kwargs):
        calls.append(device_udid)
        return {"status": "error", "message": "no tunnel"}

    monkeypatch.setattr(manager, "_start_tunnel_inner", failing)

    await manager.start_tunnel(device_udid="ATV-1")
    await manager.start_tunnel(device_udid="ATV-2")

    assert calls == ["ATV-1", "ATV-2"]


def test_failure_cache_window_is_short():
    """Long enough to dedupe one install, short enough that waking the Apple TV
    and retrying still works."""
    assert 30 <= TUNNEL_FAILURE_CACHE_SECONDS <= 120


def test_uptime_no_longer_gates_recovery():
    """The stale-uptime constant is gone: recovery is free, so it is always
    attempted rather than reserved for daemons deemed 'old'."""
    import catapult.device as device

    assert not hasattr(device, "STALE_TUNNELD_UPTIME_S")


def test_recovery_does_not_reinstall_the_daemon():
    """_recover_tunneld must not reach the privileged installer."""
    import inspect

    source = inspect.getsource(DeviceManager._recover_tunneld)

    assert "_install_tunneld_launchdaemon" not in source
    assert "clear_tunnels" in source and "shutdown" in source
