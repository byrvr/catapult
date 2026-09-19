"""Discovery used to show one device as several rows, and call anything it could
not classify an "Apple Device". These cover the shapes that produced that."""

from catapult.device import (
    device_class_for,
    discovery_identity,
    is_local_machine,
    merge_discovered,
    preferred_address,
)

LOCAL_TOKENS = {"ruslansmacbookpro"}
LOCAL_ADDRESSES = {"192.168.100.196", "fe80::1"}


def record(**kw):
    base = {
        "name": "",
        "model": "",
        "udid": "",
        "host": "",
        "addresses": [],
        "port": 7000,
        "hostname": "",
        "service": "_airplay._tcp.local.",
        "device_class": "unknown",
        "connection": "network",
        "installable": False,
        "needs_setup": False,
        "paired": False,
        "requires_tunnel": False,
        "tunnel_active": False,
        "properties": {},
    }
    base.update(kw)
    return base


def merged(raw):
    return merge_discovered(raw, local_tokens=LOCAL_TOKENS, local_addresses=LOCAL_ADDRESSES)


# ── classification ──

def test_lg_webos_tv_is_not_called_an_apple_device():
    assert device_class_for(name="[LG] webOS TV QNED82A6B", model="") == "airplay"


def test_other_airplay_vendors_are_recognised_too():
    for name in ("Samsung Tizen TV", "Roku Streaming Stick", "SONOS Beam", "BRAVIA 55"):
        assert device_class_for(name=name, model="") == "airplay", name


def test_apple_devices_still_win_over_the_vendor_check():
    assert device_class_for(name="Living Room", model="AppleTV6,2") == "tvos"
    assert device_class_for(name="Ruslan's iPhone", model="iPhone15,2") == "ios"


def test_a_vendor_word_inside_a_person_name_is_not_a_match():
    # "Olga" contains "lg"; word boundaries keep it an unknown, not an AirPlay TV.
    assert device_class_for(name="Olga's iPad", model="iPad13,1") == "ipados"
    assert device_class_for(name="Olga", model="") == "unknown"


# ── addresses ──

def test_routable_ipv4_beats_link_local_ipv6():
    assert preferred_address(["fe80::1%en0", "192.168.100.92"]) == "192.168.100.92"


def test_routable_ipv6_beats_link_local():
    assert preferred_address(["fe80::1", "2001:db8::5"]) == "2001:db8::5"


def test_loopback_is_never_chosen():
    assert preferred_address(["127.0.0.1", "::1", "10.0.0.4"]) == "10.0.0.4"


# ── the local Mac ──

def test_this_mac_is_dropped_by_name():
    raw = [record(name="Ruslan's MacBook Pro", host="fe80::1", addresses=["fe80::1"])]
    assert merged(raw) == []


def test_this_mac_is_dropped_by_address():
    raw = [record(name="Something Else", host="192.168.100.196", addresses=["192.168.100.196"])]
    assert merged(raw) == []


def test_a_second_mac_is_kept():
    raw = [record(name="Office iMac", model="iMac21,1", host="192.168.100.50",
                  addresses=["192.168.100.50"])]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["device_class"] == "macos"


# ── one device, one row ──

def test_one_device_on_two_addresses_collapses_to_one_row():
    raw = [
        record(name="Living Room", model="AppleTV6,2", host="fe80::aaaa",
               addresses=["fe80::aaaa"], service="_airplay._tcp.local.",
               properties={"deviceid": "AA:BB:CC:DD:EE:FF"}),
        record(name="Living Room", model="", host="192.168.100.92",
               addresses=["192.168.100.92"], service="_remotepairing._tcp.local.",
               needs_setup=True, properties={"deviceid": "aa:bb:cc:dd:ee:ff"}),
    ]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["name"] == "Living Room"
    assert out[0]["host"] == "192.168.100.92"
    assert set(out[0]["addresses"]) == {"192.168.100.92", "fe80::aaaa"}
    assert out[0]["device_class"] == "tvos"


def test_records_sharing_an_address_collapse_even_without_an_identifier():
    raw = [
        record(name="Apple Device", host="192.168.100.20", addresses=["192.168.100.20"],
               service="_airplay._tcp.local."),
        record(name="Kitchen HomePod", model="AudioAccessory5,1", host="192.168.100.20",
               addresses=["192.168.100.20"], service="_companion-link._tcp.local."),
    ]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["name"] == "Kitchen HomePod"
    assert out[0]["device_class"] == "homepod"


def test_same_name_on_two_addresses_collapses_without_an_identifier():
    raw = [
        record(name="Guest Apple TV", host="192.168.100.77", addresses=["192.168.100.77"]),
        record(name="Guest Apple TV", host="fe80::bbbb", addresses=["fe80::bbbb"]),
    ]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["host"] == "192.168.100.77"
    assert "(" not in out[0]["name"]


def test_two_different_devices_with_one_name_stay_separate():
    raw = [
        record(name="iPhone", model="iPhone15,2", host="192.168.100.31",
               addresses=["192.168.100.31"], properties={"deviceid": "11:11:11:11:11:11"}),
        record(name="iPhone", model="iPhone12,1", host="192.168.100.32",
               addresses=["192.168.100.32"], properties={"deviceid": "22:22:22:22:22:22"}),
    ]
    out = merged(raw)
    assert len(out) == 2
    assert {d["name"] for d in out} == {"iPhone (31)", "iPhone (32)"}


def test_disambiguation_never_uses_an_ipv6_tail():
    # The old suffix was the address tail, which produced names like "(0::1)".
    raw = [
        record(name="Apple TV", host="fe80::dead", addresses=["fe80::dead"],
               model="AppleTV6,2", properties={"deviceid": "33:33:33:33:33:33"}),
        record(name="Apple TV", host="fe80::beef", addresses=["fe80::beef"],
               model="AppleTV11,1", properties={"deviceid": "44:44:44:44:44:44"}),
    ]
    out = merged(raw)
    assert len(out) == 2
    assert {d["name"] for d in out} == {"Apple TV (AppleTV6,2)", "Apple TV (AppleTV11,1)"}
    for d in out:
        assert "::" not in d["name"]


# ── identity + install rules preserved ──

def test_identity_prefers_a_real_identifier_over_the_address():
    assert discovery_identity(record(properties={"UniqueDeviceID": "ABC"})) == "id:abc"
    assert discovery_identity(record(properties={"deviceid": "AA:BB"})) == "id:aa:bb"
    assert discovery_identity(record(host="10.0.0.1")) == ""


def test_a_phone_on_mobdev2_is_still_not_directly_installable():
    raw = [record(name="Ruslan's iPhone", model="iPhone15,2", host="192.168.100.40",
                  addresses=["192.168.100.40"], service="_apple-mobdev2._tcp.local.",
                  installable=True, properties={"deviceid": "55:55:55:55:55:55"})]
    out = merged(raw)
    assert out[0]["installable"] is False
    assert out[0]["needs_setup"] is True


def test_usb_devices_are_never_treated_as_the_local_machine():
    raw = [record(name="Ruslan's MacBook Pro", host="usb", addresses=[],
                  connection="usb", udid="UDID1", installable=True)]
    out = merge_discovered(raw, local_tokens=LOCAL_TOKENS, local_addresses=LOCAL_ADDRESSES)
    assert len(out) == 1
    assert out[0]["installable"] is True


def test_two_usb_devices_do_not_merge_on_a_shared_placeholder_host():
    raw = [
        record(name="iPad A", model="iPad13,1", host="usb", connection="usb", udid="U1"),
        record(name="iPad B", model="iPad14,1", host="usb", connection="usb", udid="U2"),
    ]
    assert len(merge_discovered(raw, local_tokens=set(), local_addresses=set())) == 2


def test_is_local_machine_ignores_an_empty_token_set():
    assert not is_local_machine(record(name="Living Room", host="192.168.1.5"), set(), set())


# ── naming a device that advertises nothing ──

from catapult.device import _Listener, human_model_name


class FakeInfo:
    def __init__(self, props, addresses, port=7000, server=""):
        self.properties = {k.encode(): v.encode() for k, v in props.items()}
        self.port = port
        self.server = server
        self._addresses = addresses

    def parsed_scoped_addresses(self):
        return self._addresses


class FakeZeroconf:
    def __init__(self, info):
        self._info = info

    def get_service_info(self, stype, name):
        return self._info


def listen(stype, name, props, addresses=("192.168.100.20",), server=""):
    listener = _Listener()
    listener.add_service(
        FakeZeroconf(FakeInfo(props, list(addresses), server=server)), stype, name
    )
    return list(listener.found.values())


def test_model_codes_become_human_names():
    assert human_model_name("AppleTV14,1") == "Apple TV 4K (3rd gen)"
    assert human_model_name("AudioAccessory5,1") == "HomePod mini"


def test_unmapped_codes_fall_back_to_the_family():
    assert human_model_name("AppleTV99,9") == "Apple TV"
    assert human_model_name("MacBookPro18,3") == "MacBook Pro"
    assert human_model_name("iPhone15,2") == "iPhone"


def test_a_model_we_cannot_read_gives_nothing_rather_than_a_guess():
    assert human_model_name("J305AP") == ""
    assert human_model_name("") == ""


def test_airplay_advertises_its_model_under_am():
    found = listen("_airplay._tcp.local.", "Living Room._airplay._tcp.local.",
                   {"am": "AppleTV14,1", "deviceid": "AA:BB:CC:DD:EE:FF"})
    assert found[0]["model"] == "AppleTV14,1"
    assert found[0]["device_class"] == "tvos"


def test_device_info_records_are_marked_as_descriptions_only():
    found = listen("_device-info._tcp.local.", "box._device-info._tcp.local.",
                   {"model": "AppleTV14,1"})
    assert found[0]["info_only"] is True


def test_a_description_alone_is_not_a_device():
    raw = [record(name="box", model="AppleTV14,1", host="192.168.100.20",
                  addresses=["192.168.100.20"], service="_device-info._tcp.local.",
                  info_only=True)]
    assert merged(raw) == []


def test_a_nameless_remotepairing_device_is_named_by_its_descriptor():
    # Exactly the row from the screenshot: a rotating UUID for a name, no model,
    # and a _device-info record on the same address that knows what it is.
    raw = [
        record(name="Apple Device", host="192.168.100.20", addresses=["192.168.100.20"],
               service="_remotepairing._tcp.local.", needs_setup=True,
               udid="9E108FA2-5C19-43F5-B04A-71ABC57B4677._remotepairing._tcp.local."),
        record(name="box", model="AppleTV14,1", host="192.168.100.20",
               addresses=["192.168.100.20"], service="_device-info._tcp.local.",
               info_only=True),
    ]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["name"] == "Apple TV 4K (3rd gen)"
    assert out[0]["device_class"] == "tvos"
    # the real row wins, so Setup still works
    assert out[0]["service"] == "_remotepairing._tcp.local."
    assert out[0]["needs_setup"] is True


def test_a_real_name_still_beats_the_model():
    raw = [
        record(name="Living Room", host="192.168.100.92", addresses=["192.168.100.92"],
               service="_remotepairing._tcp.local.", needs_setup=True),
        record(name="livingroom", model="AppleTV14,1", host="192.168.100.92",
               addresses=["192.168.100.92"], service="_device-info._tcp.local.",
               info_only=True),
    ]
    out = merged(raw)
    assert out[0]["name"] == "Living Room"


def test_an_undecodable_model_falls_back_to_the_host_name():
    raw = [
        record(name="Apple Device", host="192.168.100.20", addresses=["192.168.100.20"],
               service="_remotepairing._tcp.local.", needs_setup=True),
        record(name="box", model="J305AP", host="192.168.100.20",
               addresses=["192.168.100.20"], service="_device-info._tcp.local.",
               info_only=True),
    ]
    out = merged(raw)
    # "box" is the Bonjour host name; still more use than "Apple Device".
    assert out[0]["name"] == "box"


def test_two_nameless_devices_do_not_collapse_into_one():
    # Every device that advertises no name is called "Apple Device". Merging on
    # that placeholder made a second Apple TV vanish from the picker entirely.
    raw = [
        record(name="Apple Device", host="192.168.100.20", addresses=["192.168.100.20"],
               service="_remotepairing._tcp.local.", needs_setup=True),
        record(name="Apple Device", host="192.168.100.22", addresses=["192.168.100.22"],
               service="_remotepairing._tcp.local.", needs_setup=True),
    ]
    out = merged(raw)
    assert len(out) == 2
    assert {d["host"] for d in out} == {"192.168.100.20", "192.168.100.22"}


def test_a_nameless_device_does_not_swallow_a_named_one():
    raw = [
        record(name="Living Room", model="AppleTV14,1", host="192.168.100.92",
               addresses=["192.168.100.92"], service="_airplay._tcp.local."),
        record(name="Apple Device", host="192.168.100.92", addresses=["192.168.100.92"],
               service="_remotepairing._tcp.local.", needs_setup=True),
        record(name="Apple Device", host="192.168.100.20", addresses=["192.168.100.20"],
               service="_remotepairing._tcp.local.", needs_setup=True),
    ]
    out = merged(raw)
    assert len(out) == 2, [d["name"] for d in out]
    by_host = {d["host"]: d for d in out}
    assert by_host["192.168.100.92"]["name"] == "Living Room"
    assert by_host["192.168.100.20"]["name"] == "Apple Device"


def test_wifi_sync_identifies_a_phone_that_advertises_nothing_else():
    # _apple-mobdev2 is advertised only by an iPhone or iPad. The record carries
    # no name and no model, so without the service the device came back
    # "unknown" and was labelled "Apple Device" — and every check that asks
    # "is this a phone?" answered no.
    found = listen("_apple-mobdev2._tcp.local.",
                   "aa:bb@fe80::1-supportsRP-26._apple-mobdev2._tcp.local.",
                   {}, addresses=("192.168.100.20", "fe80::1033:1faf:b81c:b30f"))
    assert found[0]["device_class"] == "iosfamily"
    assert found[0]["host"] == "192.168.100.20"


def test_a_real_model_still_wins_over_the_service():
    found = listen("_apple-mobdev2._tcp.local.", "x._apple-mobdev2._tcp.local.",
                   {"model": "iPad13,1"})
    assert found[0]["device_class"] == "ipados"


def test_the_service_class_survives_the_merge():
    raw = [record(name="Apple Device", host="192.168.100.20",
                  addresses=["192.168.100.20"],
                  service="_apple-mobdev2._tcp.local.",
                  device_class="iosfamily", needs_setup=True)]
    assert merged(raw)[0]["device_class"] == "iosfamily"


def test_an_apple_tv_is_not_reclassified_by_the_service_map():
    raw = [record(name="Living Room", model="AppleTV14,1", host="192.168.100.92",
                  addresses=["192.168.100.92"],
                  service="_remotepairing._tcp.local.", needs_setup=True)]
    assert merged(raw)[0]["device_class"] == "tvos"


# ── the Bonjour hostname: a name, and the key that ties records together ──

from catapult.device import bonjour_hostname


def test_hostname_is_stripped_to_something_showable():
    assert bonjour_hostname("Ruslans-iPhone.local.") == "Ruslans-iPhone"
    assert bonjour_hostname("Living-Room.local") == "Living-Room"
    assert bonjour_hostname("") == ""


def test_wifi_sync_falls_back_to_the_hostname_for_a_name():
    # The only thing this record carries besides an address.
    found = listen("_apple-mobdev2._tcp.local.",
                   "7a:8b:06:7f:5d:25@fe80::788b:6ff:fe7f:5d25-supportsRP-26._apple-mobdev2._tcp.local.",
                   {}, addresses=("192.168.100.39",), server="Ruslans-iPhone.local.")
    assert found[0]["name"] == "Ruslans-iPhone"
    assert found[0]["hostname"] == "Ruslans-iPhone"
    # Better than the service alone could manage: the hostname names the model
    # family outright, so this is an iPhone rather than "iPhone or iPad".
    assert found[0]["device_class"] == "ios"


def test_a_chosen_name_still_beats_the_hostname():
    found = listen("_companion-link._tcp.local.", "Living Room._companion-link._tcp.local.",
                   {"model": "AppleTV14,1"}, server="Living-Room.local.")
    assert found[0]["name"] == "Living Room"


def test_a_device_info_record_survives_having_no_address():
    # These describe a host and carry no address, and requiring one dropped
    # every one of them — which is why browsing the service found nothing.
    found = listen("_device-info._tcp.local.", "Ruslans-iPhone._device-info._tcp.local.",
                   {"model": "iPhone15,2"}, addresses=(), server="Ruslans-iPhone.local.")
    assert len(found) == 1
    assert found[0]["model"] == "iPhone15,2"
    assert found[0]["info_only"] is True
    assert found[0]["host"] == ""


def test_a_record_with_neither_address_nor_hostname_is_still_dropped():
    assert listen("_airplay._tcp.local.", "x._airplay._tcp.local.", {}, addresses=()) == []


def test_device_info_hands_its_model_to_the_device_it_describes():
    raw = [
        record(name="Ruslans-iPhone", host="192.168.100.39",
               addresses=["192.168.100.39"], hostname="Ruslans-iPhone",
               service="_apple-mobdev2._tcp.local.", device_class="iosfamily",
               needs_setup=True),
        record(name="Ruslans-iPhone", model="iPhone15,2", host="", addresses=[],
               hostname="Ruslans-iPhone", service="_device-info._tcp.local.",
               info_only=True),
    ]
    out = merged(raw)
    assert len(out) == 1
    assert out[0]["model"] == "iPhone15,2"
    assert out[0]["device_class"] == "ios"
    assert out[0]["host"] == "192.168.100.39"
    assert out[0]["name"] == "Ruslans-iPhone"


def test_hostnames_that_differ_do_not_merge():
    raw = [
        record(name="A", host="192.168.1.5", addresses=["192.168.1.5"], hostname="phone-a"),
        record(name="B", host="192.168.1.6", addresses=["192.168.1.6"], hostname="phone-b"),
    ]
    assert len(merged(raw)) == 2


# What Wi-Fi sync actually calls its record: a MAC, an address and a suffix.
# It is filtered out as a name, which is exactly why the hostname matters.
MOBDEV_INSTANCE = "7a:8b:06:7f:5d:25@fe80::788b:6ff:fe7f:5d25-supportsRP-26._apple-mobdev2._tcp.local."


def test_an_ipad_hostname_is_read_as_an_ipad():
    found = listen("_apple-mobdev2._tcp.local.", MOBDEV_INSTANCE, {},
                   addresses=("192.168.100.41",), server="Ruslans-iPad.local.")
    assert found[0]["device_class"] == "ipados"


def test_a_hostname_that_names_no_model_still_falls_back_to_the_service():
    found = listen("_apple-mobdev2._tcp.local.", MOBDEV_INSTANCE, {},
                   addresses=("192.168.100.43",), server="Telefon.local.")
    assert found[0]["name"] == "Telefon"
    assert found[0]["device_class"] == "iosfamily"


def test_the_wifi_sync_instance_name_is_never_shown_as_a_name():
    found = listen("_apple-mobdev2._tcp.local.", MOBDEV_INSTANCE, {},
                   addresses=("192.168.100.43",))
    assert "supportsRP" not in found[0]["name"]
    assert "@" not in found[0]["name"]
