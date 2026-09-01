"""Device classification and tunnel selection.

The Apple TV path came first and its assumptions leaked into code that any
device now goes through. The worst consequence: selecting a Wi-Fi iPad could
install the app onto the Apple TV, because _select_rsd falls back to "the only
tunnel we have" when nothing scores.
"""

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
