"""Device classification and tunnel selection.

The Apple TV path came first and its assumptions leaked into code that any
device now goes through. The worst consequence: selecting a Wi-Fi iPad could
install the app onto the Apple TV, because _select_rsd falls back to "the only
tunnel we have" when nothing scores.
"""

import pytest

from catapult.device import DeviceManager, device_class_for


class FakeRSD:
    def __init__(self, udid="", product_type=""):
        self.udid = udid
        self.product_type = product_type
        self.peer_info = {"Properties": {"ProductType": product_type}}


def test_classifies_ipad_from_product_type():
    assert device_class_for(name="Ruslan's Tablet", model="iPad13,4") == "ipados"


def test_classifies_ipad_from_name():
    assert device_class_for(name="Work iPad", model="") == "ipados"


def test_classifies_apple_tv():
    assert device_class_for(name="Living Room", model="AppleTV14,1") == "tvos"


def test_classifies_iphone():
    assert device_class_for(name="Ruslan's iPhone", model="iPhone16,2") == "ios"


def test_unknown_when_nothing_identifies_it():
    assert device_class_for(name="Bedroom", model="") == "unknown"


def test_ipad_never_classified_as_tvos_from_a_generic_name():
    """A remembered device used to come back as tvos or unknown, never ipados."""
    assert device_class_for(name="", model="iPad14,6") == "ipados"


def test_select_rsd_matches_on_udid():
    manager = DeviceManager()
    rsds = [FakeRSD(udid="AAA"), FakeRSD(udid="BBB")]

    chosen = manager._select_rsd(rsds, {"udid": "BBB"}, allow_single_fallback=True)

    assert chosen.udid == "BBB"


def test_select_rsd_refuses_to_fall_back_across_device_classes():
    """The dangerous case: one Apple TV tunnel is up and the user picks an iPad.
    Installing to the only tunnel available would install onto the Apple TV."""
    manager = DeviceManager()
    apple_tv = FakeRSD(udid="TV-UDID", product_type="AppleTV14,1")
    target = {"udid": "IPAD-MDNS-ID", "device_class": "ipados", "model": "iPad13,4"}

    chosen = manager._select_rsd([apple_tv], target, allow_single_fallback=True)

    assert chosen is None


def test_select_rsd_still_falls_back_for_a_matching_class():
    """Apple TV mDNS records rotate their identifiers, so the fallback has to
    survive for the case it was written for."""
    manager = DeviceManager()
    apple_tv = FakeRSD(udid="TV-UDID", product_type="AppleTV14,1")
    target = {"udid": "ROTATED-ID", "device_class": "tvos", "model": "AppleTV14,1"}

    chosen = manager._select_rsd([apple_tv], target, allow_single_fallback=True)

    assert chosen is apple_tv


def test_select_rsd_fallback_allowed_when_target_class_is_unknown():
    """An unclassified target should not be blocked — only a known mismatch is."""
    manager = DeviceManager()
    apple_tv = FakeRSD(udid="TV-UDID", product_type="AppleTV14,1")

    chosen = manager._select_rsd([apple_tv], {"udid": "X"}, allow_single_fallback=True)

    assert chosen is apple_tv


def test_select_rsd_returns_none_without_fallback():
    manager = DeviceManager()
    rsds = [FakeRSD(udid="AAA"), FakeRSD(udid="BBB")]

    assert manager._select_rsd(rsds, {"udid": "ZZZ"}, allow_single_fallback=False) is None


def test_usb_device_stays_installable_through_dedupe():
    """Regression: the mDNS-only guard also fired on usbmux devices, so a
    trusted iPad on a cable was marked 'needs setup' and could not be selected."""
    from catapult.device import INSTALLABLE_SERVICES, TUNNEL_DEVICE_CLASSES, device_class_for

    usb_ipad = {
        "name": "Ruslan's iPad",
        "model": "iPad16,4",
        "service": "usbmux",
        "device_class": "ipados",
        "installable": True,
        "needs_setup": False,
    }
    merged_class = device_class_for(name=usb_ipad["name"], model=usb_ipad["model"])
    should_downgrade = (
        usb_ipad["service"] in INSTALLABLE_SERVICES
        and merged_class not in TUNNEL_DEVICE_CLASSES
    )

    assert merged_class == "ipados"
    assert not should_downgrade


def test_mdns_ipad_is_still_downgraded():
    from catapult.device import INSTALLABLE_SERVICES, TUNNEL_DEVICE_CLASSES, device_class_for

    mdns_ipad = {"name": "Ruslan's iPad", "model": "iPad16,4",
                 "service": "_apple-mobdev2._tcp.local."}
    merged_class = device_class_for(name=mdns_ipad["name"], model=mdns_ipad["model"])

    assert mdns_ipad["service"] in INSTALLABLE_SERVICES
    assert merged_class not in TUNNEL_DEVICE_CLASSES


class _FakeMux:
    serial = "00008030-000A1B2C3D4E5F60"
    connection_type = "USB"


def _fake_usbmux(monkeypatch, *, paired: bool):
    import pymobiledevice3.lockdown as lockdown_mod
    import pymobiledevice3.usbmux as usbmux_mod

    class FakeLockdown:
        def __init__(self):
            self.paired = paired

        async def get_value(self, key=None):
            # lockdown answers the basic keys before pairing, trusted or not.
            return {"ProductType": "iPad13,4", "DeviceClass": "iPad", "DeviceName": "Tablet"}.get(key)

    async def list_devices():
        return [_FakeMux()]

    async def create_using_usbmux(**kwargs):
        assert kwargs.get("autopair") is False, "discovery must never trigger the Trust dialog"
        return FakeLockdown()

    monkeypatch.setattr(usbmux_mod, "list_devices", list_devices)
    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", create_using_usbmux)


async def test_untrusted_usb_device_is_not_reported_installable(monkeypatch):
    """create_using_usbmux(autopair=False) returns a client for an UNTRUSTED
    device without raising, so 'the call succeeded' meant every plugged-in
    iPhone was reported trusted and installable. Only a validated pair record
    counts."""
    _fake_usbmux(monkeypatch, paired=False)

    (device,) = await DeviceManager()._scan_usb_devices()

    assert device["installable"] is False
    assert device["needs_setup"] is True
    assert "Trust" in device["setup_hint"]
    assert device["device_class"] == "ipados"


async def test_trusted_usb_device_is_installable(monkeypatch):
    _fake_usbmux(monkeypatch, paired=True)

    (device,) = await DeviceManager()._scan_usb_devices()

    assert device["installable"] is True
    assert device["needs_setup"] is False
    assert device["setup_hint"] == ""


# ── Trust via Setup ───────────────────────────────────────────────────────────

def _usb_row(udid="USB1", device_class="ipados"):
    return {
        "udid": udid, "name": "Tablet", "host": f"usb:{udid}", "port": 62078,
        "service": "usbmux", "device_class": device_class,
        "installable": False, "needs_setup": True,
        "setup_hint": "Unlock this iPad and tap Trust, then scan again.",
    }


async def test_setup_on_an_untrusted_usb_device_requests_pairing(monkeypatch):
    """Discovery uses autopair=False so scanning never pops the Trust dialog.
    That leaves exactly one user-initiated place to trigger it: Setup."""
    import pymobiledevice3.lockdown as lockdown_mod

    seen = {}

    class FakeLockdown:
        paired = True

    async def create_using_usbmux(**kwargs):
        seen.update(kwargs)
        return FakeLockdown()

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", create_using_usbmux)
    manager = DeviceManager()
    manager._cache["USB1"] = _usb_row()

    result = await manager.pair_device(device_udid="USB1")

    assert result["status"] == "ok"
    assert seen["serial"] == "USB1"
    assert seen["autopair"] is True
    assert manager._cache["USB1"]["installable"] is True
    assert manager._cache["USB1"]["needs_setup"] is False


async def test_setup_reports_when_trust_was_declined(monkeypatch):
    import pymobiledevice3.lockdown as lockdown_mod

    class FakeLockdown:
        paired = False

    async def create_using_usbmux(**kwargs):
        return FakeLockdown()

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", create_using_usbmux)
    manager = DeviceManager()
    manager._cache["USB1"] = _usb_row()

    result = await manager.pair_device(device_udid="USB1")

    assert result["status"] == "error"
    assert "Trust" in result["message"]
    assert manager._cache["USB1"]["installable"] is False


async def test_setup_on_a_wifi_iphone_still_says_to_use_a_cable():
    manager = DeviceManager()
    manager._cache["NET1"] = {
        "udid": "NET1", "name": "Phone", "host": "10.0.0.9", "port": 62078,
        "service": "_apple-mobdev2._tcp.local.", "device_class": "ios",
        "installable": False, "needs_setup": True,
    }

    result = await manager.pair_device(device_udid="NET1")

    assert result["status"] == "error"
    assert "cable" in result["message"]


# ── The cached tunnel belongs to one Apple TV ────────────────────────────────

from types import SimpleNamespace  # noqa: E402


def _tv_row(udid, host, name):
    return {"udid": udid, "name": name, "host": host, "port": 49152,
            "service": "_remotepairing._tcp.local.", "device_class": "tvos"}


def _tunneled(udid="UDID-A"):
    rsd = FakeRSD(udid=udid, product_type="AppleTV14,1")
    rsd.service = SimpleNamespace(address=("fd00::1", 49152))
    return rsd


async def test_cached_tunnel_is_reused_for_the_same_apple_tv(monkeypatch):
    import pymobiledevice3.tunneld.api as api

    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    manager._cache_active_tunnel(_tunneled("UDID-A"), manager._cache["TV-A"])

    async def listed(address, port):
        return True

    async def must_not_dial():
        raise AssertionError("the cached identity should have been used")

    monkeypatch.setattr(manager, "_tunnel_listed", listed)
    monkeypatch.setattr(api, "get_tunneld_devices", must_not_dial)

    assert await manager.get_real_udid(device_udid="TV-A") == ("UDID-A", "tvOS")


async def test_cached_tunnel_is_not_reused_for_a_different_apple_tv(monkeypatch):
    """Two Apple TVs, tunnel A cached, user selects B: registering B under A's
    UDID signs a profile that does not include B, and the install fails. The
    cache is bypassed and B is found by its own identifier."""
    import pymobiledevice3.tunneld.api as api

    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    manager._cache["TV-B"] = _tv_row("TV-B", "10.0.0.6", "Bedroom")
    manager._cache_active_tunnel(_tunneled("UDID-A"), manager._cache["TV-A"])

    async def listed(address, port):
        return True

    async def get_tunneld_devices():
        return [_tunneled("TV-B")]

    monkeypatch.setattr(manager, "_tunnel_listed", listed)
    monkeypatch.setattr(api, "get_tunneld_devices", get_tunneld_devices)

    udid, _ = await manager.get_real_udid(device_udid="TV-B")

    assert udid == "TV-B"


async def test_with_two_apple_tvs_an_unidentifiable_sole_tunnel_is_refused(monkeypatch):
    """Same setup, but the only live tunnel cannot be tied to B. Guessing would
    be a coin flip between two devices, so the install stops with a clear error
    instead of landing on the wrong Apple TV."""
    import pymobiledevice3.tunneld.api as api

    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    manager._cache["TV-B"] = _tv_row("TV-B", "10.0.0.6", "Bedroom")

    async def get_tunneld_devices():
        return [_tunneled("UDID-UNKNOWN")]

    monkeypatch.setattr(api, "get_tunneld_devices", get_tunneld_devices)

    with pytest.raises(RuntimeError, match="matched the selected Apple TV"):
        await manager.get_real_udid(device_udid="TV-B")


async def test_start_tunnel_does_not_answer_already_active_for_another_apple_tv(monkeypatch):
    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    manager._cache["TV-B"] = _tv_row("TV-B", "10.0.0.6", "Bedroom")
    manager._cache_active_tunnel(_tunneled("UDID-A"), manager._cache["TV-A"])
    polled = []

    async def listed(address, port):
        return True

    async def ensure():
        return True

    async def poll(target, attempts=30):
        polled.append((target or {}).get("udid"))
        return True

    monkeypatch.setattr(manager, "_tunnel_listed", listed)
    monkeypatch.setattr(manager, "_ensure_tunneld", ensure)
    monkeypatch.setattr(manager, "_poll_for_tunnel", poll)

    result = await manager._start_tunnel_inner(device_udid="TV-B")

    assert result["status"] == "ok"
    assert polled == ["TV-B"], "must look for B's tunnel instead of trusting A's cache"


# ── Two Apple TVs: never guess ────────────────────────────────────────────────

def test_select_rsd_refuses_the_sole_tunnel_when_two_apple_tvs_are_known():
    """With one Apple TV, 'the only tunnel we have' is a safe guess when its
    mDNS identifier has rotated. With two known Apple TVs it is a coin flip,
    and the losing side installs onto the wrong device."""
    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    manager._cache["TV-B"] = _tv_row("TV-B", "10.0.0.6", "Bedroom")
    sole = FakeRSD(udid="UDID-A", product_type="AppleTV14,1")

    assert manager._select_rsd([sole], manager._cache["TV-B"], allow_single_fallback=True) is None


def test_select_rsd_still_falls_back_with_a_single_known_apple_tv():
    manager = DeviceManager()
    manager._cache["TV-A"] = _tv_row("TV-A", "10.0.0.5", "Living Room")
    sole = FakeRSD(udid="UDID-A", product_type="AppleTV14,1")
    target = {"udid": "ROTATED", "host": "10.0.0.5", "device_class": "tvos"}

    assert manager._select_rsd([sole], target, allow_single_fallback=True) is sole


# ── Trust request must finish inside the client's request timeout ────────────

async def test_trust_request_waits_less_than_the_clients_timeout(monkeypatch):
    """The native client gives /api/devices/setup 60 seconds. A 90-second pair
    timeout meant the UI reported a failure while the device still showed the
    Trust prompt."""
    import pymobiledevice3.lockdown as lockdown_mod

    seen = {}

    class FakeLockdown:
        paired = True

    async def create_using_usbmux(**kwargs):
        seen.update(kwargs)
        return FakeLockdown()

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", create_using_usbmux)
    manager = DeviceManager()
    manager._cache["USB1"] = _usb_row()

    await manager.pair_device(device_udid="USB1")

    assert seen["pair_timeout"] <= 45


# ── Background installs never escalate ───────────────────────────────────────

async def test_install_forwards_allow_escalation_to_start_tunnel(monkeypatch):
    manager = DeviceManager()
    seen = {}

    async def get_device_info(udid):
        return {"udid": udid, "name": "TV", "host": "10.0.0.5", "port": 49152,
                "service": "_remotepairing._tcp.local.", "installable": False}

    async def start_tunnel(**kwargs):
        seen.update(kwargs)
        return {"status": "error", "message": "no tunnel"}

    monkeypatch.setattr(manager, "get_device_info", get_device_info)
    monkeypatch.setattr(manager, "start_tunnel", start_tunnel)

    try:
        await manager.install("TV1", "/nonexistent.ipa", allow_escalation=False)
    except Exception:
        pass

    assert seen.get("allow_escalation") is False


async def test_get_real_udid_forwards_allow_escalation_to_start_tunnel(monkeypatch):
    import pymobiledevice3.tunneld.api as api

    manager = DeviceManager()
    seen = {}

    async def no_devices():
        return []

    async def start_tunnel(**kwargs):
        seen.update(kwargs)
        return {"status": "error", "message": "no tunnel"}

    monkeypatch.setattr(api, "get_tunneld_devices", no_devices)
    monkeypatch.setattr(manager, "start_tunnel", start_tunnel)

    try:
        await manager.get_real_udid(device_udid="TV1", allow_escalation=False)
    except Exception:
        pass

    assert seen.get("allow_escalation") is False
