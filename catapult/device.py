"""Network device discovery, pairing, tunnel, and app installation."""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

logger = logging.getLogger(__name__)

MDNS_SERVICES = [
    "_remotepairing._tcp.local.",
    "_apple-mobdev2._tcp.local.",
    "_companion-link._tcp.local.",
    "_airplay._tcp.local.",
]

DEVICE_CLASS_MAP = {
    "AppleTV": "tvos",
    "iPhone": "ios",
    "iPad": "ipados",
    "Mac": "macos",
    "MacBook": "macos",
    "iMac": "macos",
    "AudioAccessory": "homepod",
    "HomePod": "homepod",
}

# Only mobdev2 is directly installable. Remotepairing needs pair + tunnel first.
INSTALLABLE_SERVICES = {"_apple-mobdev2._tcp.local."}
NEEDS_SETUP_SERVICES = {"_remotepairing._tcp.local."}

# Device classes that reach installd over an RSD tunnel. iPhone and iPad reach
# it over classic lockdown instead, via usbmux.
TUNNEL_DEVICE_CLASSES = {"tvos"}


def device_class_for(name: str, model: str) -> str:
    """Best-effort device class from an advertised name and model.

    Checks the model first: a user-chosen name like "Ruslan's Tablet" carries
    no signal, while ProductType always does. Several call sites used to
    hardcode `"tvos" if "AppleTV" in model else "unknown"`, so a remembered
    iPad could never come back as ipados.
    """
    for haystack in (str(model or ""), f"{name or ''} {model or ''}"):
        lowered = haystack.lower()
        for prefix, cls in DEVICE_CLASS_MAP.items():
            if prefix.lower() in lowered:
                return cls
    return "unknown"


STATE_DIR = Path.home() / ".catapult"
PAIRED_DEVICES_PATH = STATE_DIR / "paired_devices.json"
TUNNELD_DAEMON_LABEL = "com.catapult.tunneld"
TUNNELD_DAEMON_PLIST = Path("/Library/LaunchDaemons") / f"{TUNNELD_DAEMON_LABEL}.plist"
TUNNELD_LOG_PATH = "/tmp/catapult_tunneld.log"
# A healthy tunneld re-discovers Wi-Fi devices within seconds. If an adopted
# daemon has been up longer than this and still can't produce a tunnel while we
# are actively connecting, treat it as wedged and force a restart rather than
# blindly trusting its HTTP port. (A long-lived daemon can silently stop
# discovering devices after sleep/wake or network changes.)
STALE_TUNNELD_UPTIME_S = 3600
# Poll windows (each attempt waits 2s). An already-running healthy daemon
# usually has the tunnel up already, so a short window is enough. A freshly
# (re)started daemon is cold — it must browse mDNS, discover the device, and
# build the QUIC tunnel from scratch, which can take well over a minute — so it
# gets a much more generous window.
TUNNELD_POLL_ATTEMPTS = 30
TUNNELD_COLD_START_POLL_ATTEMPTS = 90
# Active /start-tunnel sweep over stored pairing records. Establishing a tunnel
# takes ~15-25s; a record the device rejects fails in ~10-15s (mDNS browse plus
# a refused handshake), so give each request a generous timeout but cap the
# whole sweep so a connect attempt can still fall back to the passive poll.
ACTIVE_TUNNEL_REQUEST_TIMEOUT_S = 60
ACTIVE_TUNNEL_REQUEST_BUDGET_S = 150


def _parse_ps_etime(value: str) -> float | None:
    """Parse macOS ``ps -o etime`` (``[[dd-]hh:]mm:ss``) into seconds."""
    value = value.strip()
    if not value:
        return None
    days = 0
    if "-" in value:
        day_part, value = value.split("-", 1)
        if not day_part.isdigit():
            return None
        days = int(day_part)
    try:
        nums = [int(p) for p in value.split(":")]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    hours, minutes, seconds = nums[-3], nums[-2], nums[-1]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _normalize_usbmux_connection(connection_type: object) -> str:
    raw = str(connection_type or "").strip().lower()
    if "usb" in raw:
        return "usb"
    if any(token in raw for token in ("network", "wifi", "wi-fi", "tcp")):
        return "network"
    # usbmuxd can expose Wi-Fi-paired iOS devices. Avoid calling an unknown
    # transport physical USB unless the mux record explicitly says USB.
    return "network"


def scan_network(timeout: float = 6.0) -> list[dict]:
    zc = Zeroconf()
    listener = _Listener()
    try:
        for svc in MDNS_SERVICES:
            ServiceBrowser(zc, svc, listener)
        import time
        time.sleep(timeout)
        return list(listener.found.values())
    finally:
        zc.close()


class _Listener(ServiceListener):
    def __init__(self):
        self.found: dict[str, dict] = {}

    def add_service(self, zc: Zeroconf, stype: str, name: str):
        info = zc.get_service_info(stype, name)
        if not info:
            return
        addresses = [a for a in info.parsed_scoped_addresses() if a and not a.startswith("127.")]
        if not addresses:
            return

        props = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in info.properties.items()
        }

        # Friendly name is in the service name (e.g. "Living Room._companion-link...")
        friendly_name = name.split(f".{stype}")[0] if f".{stype}" in name else ""
        model = props.get("model", "") or props.get("rpMd", "")

        # Filter out raw MAC/UUID/IPv6 names — they're not human-readable
        def _is_good_name(n: str) -> bool:
            if not n or len(n) > 50:
                return False
            if ":" in n or "@" in n:  # MAC or IPv6
                return False
            if len(n) > 20 and n.count("-") >= 3:  # UUID-like
                return False
            return True

        candidates = [friendly_name, props.get("deviceName", ""), model]
        device_name = next((n for n in candidates if _is_good_name(n)), "")
        if not device_name:
            # Last resort: use model family or generic label
            device_name = model.split(",")[0] if model else "Apple Device"
        udid = (props.get("UniqueDeviceID") or props.get("rpMRtID")
                or props.get("deviceid") or name)
        device_class = device_class_for(name=device_name, model=model)

        # mDNS says an iPhone/iPad is on the network; it does not say this Mac
        # can talk to it. Reaching installd needs a lockdown pair record, which
        # only a USB trust creates. Claiming installable here sent the install
        # down the Apple TV tunnel path: an admin password prompt, a ~3 minute
        # poll, and then failure — or worse, an install onto the Apple TV.
        installable = stype in INSTALLABLE_SERVICES
        needs_setup = stype in NEEDS_SETUP_SERVICES
        if installable and device_class not in TUNNEL_DEVICE_CLASSES:
            installable = False
            needs_setup = True

        key = f"{addresses[0]}:{info.port}:{stype}"
        self.found[key] = {
            "name": device_name,
            "model": model,
            "udid": udid,
            "host": addresses[0],
            "port": info.port,
            "service": stype,
            "device_class": device_class,
            "connection": "network",
            "installable": installable,
            "needs_setup": needs_setup,
            "paired": False,
            "requires_tunnel": stype in NEEDS_SETUP_SERVICES,
            "tunnel_active": False,
            "properties": props,
        }

    def remove_service(self, *a):
        pass

    def update_service(self, zc, stype, name):
        self.add_service(zc, stype, name)


class DeviceManager:
    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._tunnel_proc: asyncio.subprocess.Process | None = None
        self._tunneled_hosts: set[str] = set()  # hosts with active tunnels
        self._paired_devices = self._load_paired_devices()
        self._pairing_lock = threading.Lock()
        self._tunnel_lock: asyncio.Lock | None = None  # created lazily in async context
        self._scan_lock: asyncio.Lock | None = None
        self._last_scan_result: list[dict] | None = None
        self._last_scan_at: float = 0.0

    SCAN_CACHE_TTL = 5.0

    def _cached_scan(self) -> list[dict] | None:
        if self._last_scan_result is None:
            return None
        if time.monotonic() - self._last_scan_at > self.SCAN_CACHE_TTL:
            return None
        return self._last_scan_result

    async def discover(self, timeout: float = 6.0, *, allow_stale: bool = False) -> list[dict]:
        # Single-flight: callers arriving while a scan is running (UI poll,
        # install preflight, diagnostics) reuse its result instead of each
        # queuing a full scan behind the lock — stacked scans were blowing
        # the endpoints' 8/15s deadlines.
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        cached = self._cached_scan()
        if cached is not None:
            return cached
        # Stale-while-revalidate: if a prior result exists (just past TTL) and
        # the caller tolerates it, return it now and refresh in the background.
        # The UI device list uses this so a slow cold scan never 504s the poll;
        # install preflight passes allow_stale=False for an accurate snapshot.
        if allow_stale and self._last_scan_result is not None:
            self._ensure_background_scan(timeout)
            return self._last_scan_result
        async with self._scan_lock:
            cached = self._cached_scan()
            if cached is not None:
                return cached
            devices = await self._discover_uncached(timeout)
            self._last_scan_result = devices
            self._last_scan_at = time.monotonic()
            return devices

    def _ensure_background_scan(self, timeout: float) -> None:
        task = getattr(self, "_bg_scan_task", None)
        if task is not None and not task.done():
            return
        async def _run():
            try:
                await self.discover(timeout)
            except Exception:
                logger.debug("Background rescan failed", exc_info=True)
        self._bg_scan_task = asyncio.create_task(_run())

    async def _discover_uncached(self, timeout: float) -> list[dict]:
        logger.info("Scanning network for devices (%ss timeout)...", timeout)
        try:
            raw = await self._scan_in_subprocess(timeout)
        except asyncio.TimeoutError:
            logger.error("mDNS scan exceeded timeout")
            raise RuntimeError("Local network scan timed out")
        except Exception:
            logger.exception("mDNS scan failed")
            raw = []

        raw.extend(await self._scan_usb_devices())

        # Deduplicate by host — prefer installable, merge best name/model
        by_host: dict[str, dict] = {}
        best_names: dict[str, str] = {}
        best_models: dict[str, str] = {}
        for d in raw:
            host = d["host"]
            n = d["name"]
            # A "good" name is short, no colons (not MAC), no dashes-only (not UUID)
            if n and len(n) < 30 and ":" not in n and not (len(n) > 20 and n.count("-") >= 3):
                best_names[host] = n
            if d.get("model"):
                best_models[host] = d["model"]
            # Priority: installable > needs_setup > other
            def _priority(dev):
                if dev["installable"]: return 2
                if dev.get("needs_setup"): return 1
                return 0
            if host not in by_host or _priority(d) > _priority(by_host[host]):
                by_host[host] = d
        for host, d in by_host.items():
            if host in best_names:
                d["name"] = best_names[host]
            if host in best_models and not d.get("model"):
                d["model"] = best_models[host]
            # Recompute device_class against the merged name+model — the
            # per-service entry that won deduplication (often _remotepairing)
            # may have arrived with neither, leaving class as "unknown".
            merged_class = device_class_for(name=d["name"], model=d.get("model", ""))
            if merged_class != "unknown":
                d["device_class"] = merged_class
                # A merged name/model can reveal an iPhone or iPad behind a
                # record that mDNS marked installable. Re-apply the same rule.
                if d["installable"] and merged_class not in TUNNEL_DEVICE_CLASSES:
                    d["installable"] = False
                    d["needs_setup"] = True

        devices = list(by_host.values())

        # Disambiguate duplicate names by appending short host suffix
        name_counts: dict[str, list[dict]] = {}
        for d in devices:
            name_counts.setdefault(d["name"], []).append(d)
        for name, group in name_counts.items():
            if len(group) > 1:
                for d in group:
                    suffix = d["host"].rsplit(".", 1)[-1] if "." in d["host"] else d["host"][-4:]
                    d["name"] = f"{name} ({suffix})"

        remote_pair_ids = self._remote_paired_identifiers()
        for d in devices:
            if self._is_known_paired(d, remote_pair_ids):
                d["paired"] = True
                d["needs_setup"] = False
                d["requires_tunnel"] = "remotepairing" in d.get("service", "")
            # Preserve tunnel state across rescans
            if d["host"] in self._tunneled_hosts:
                d["installable"] = True
                d["needs_setup"] = False
                d["paired"] = True
                d["requires_tunnel"] = "remotepairing" in d.get("service", "")
                d["tunnel_active"] = True
            self._cache[d["udid"]] = d

        logger.info("Found %d device(s)", len(devices))
        return devices

    async def _scan_usb_devices(self) -> list[dict]:
        """Discover trusted iPhone/iPad devices exposed through usbmuxd."""
        try:
            from pymobiledevice3.lockdown import create_using_usbmux
            from pymobiledevice3.usbmux import list_devices
        except Exception as e:
            logger.debug("usbmux discovery unavailable: %s", e)
            return []

        try:
            mux_devices = await list_devices()
        except Exception as e:
            logger.info("usbmux discovery failed: %s", e)
            return []

        devices: list[dict] = []
        for mux_device in mux_devices:
            udid = getattr(mux_device, "serial", "") or ""
            if not udid:
                continue

            connection_type_raw = getattr(mux_device, "connection_type", "")
            connection = _normalize_usbmux_connection(connection_type_raw)
            connection_type = str(connection_type_raw or connection).strip()
            # Deliberately blank rather than defaulting to iPhone: when the
            # lockdown probe below fails on an untrusted device these defaults
            # survive, and an iPad would be shown as an iPhone.
            info = {
                "DeviceName": "",
                "ProductType": "",
                "ProductVersion": "",
                "DeviceClass": "",
                "UniqueDeviceID": udid,
            }
            trusted = False
            try:
                # autopair=False: discovery runs on a 5s cache TTL while the UI
                # polls, and the default would re-trigger the Trust dialog on an
                # untrusted device on every single scan.
                lockdown = await create_using_usbmux(
                    serial=udid, pair_timeout=3, autopair=False
                )
                for key in list(info):
                    try:
                        value = await lockdown.get_value(key=key)
                    except Exception:
                        value = None
                    if value:
                        info[key] = str(value)
                trusted = True
            except Exception as e:
                logger.info("usbmux device %s is visible but not ready: %s", udid, e)

            device_class = device_class_for(
                name=info.get("DeviceClass", ""), model=info.get("ProductType", "")
            )
            if device_class == "unknown":
                # An untrusted device answers nothing, so we genuinely do not
                # know what it is. Say "device", not "iPhone".
                device_class = "ios" if trusted else "unknown"

            default_name = {
                "ipados": "iPad",
                "ios": "iPhone",
            }.get(device_class, "Connected device")

            devices.append({
                "name": info.get("DeviceName") or default_name,
                "model": info.get("ProductType", ""),
                "udid": udid,
                "host": f"usb:{udid}",
                "port": 62078,
                "service": "usbmux",
                "device_class": device_class,
                "connection": connection,
                "installable": trusted,
                "needs_setup": not trusted,
                "paired": trusted,
                "requires_tunnel": False,
                "tunnel_active": False,
                "properties": {
                    "ProductVersion": info.get("ProductVersion", ""),
                    "UniqueDeviceID": info.get("UniqueDeviceID", udid),
                    "ConnectionType": connection_type,
                    "trusted": trusted,
                    "setup_hint": "" if trusted else
                        "Unlock this device and tap Trust, then scan again.",
                },
            })
        return devices

    async def _scan_in_subprocess(self, timeout: float) -> list[dict]:
        # The catapult package is not installed into the venv, so the child can
        # only resolve it through its working directory. The inherited cwd may
        # be a deleted directory (app bundle rebuilt while the server runs), so
        # pin cwd and PYTHONPATH to the backend root this module lives in.
        backend_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(backend_root)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "catapult.discovery_worker",
            str(timeout),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=backend_root,
            env=env,
        )
        try:
            # +8 leaves headroom for interpreter startup and zeroconf teardown
            # under load (signing jobs run in this process during installs)
            # while staying under /api/devices' 15s deadline.
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout + 8)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise

        if proc.returncode != 0:
            message = stderr.decode(errors="replace").strip()
            raise RuntimeError(message or f"Device scan failed with exit code {proc.returncode}")

        try:
            return json.loads(stdout.decode() or "[]")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Device scan returned invalid data: {e}") from e

    async def get_device_info(self, udid: str) -> dict:
        if udid in self._cache:
            return self._cache[udid]
        devices = await self.discover()
        for d in devices:
            if d["udid"] == udid:
                return d
        remembered = self._remembered_device_info(udid)
        if remembered:
            self._cache[udid] = remembered
            return remembered
        raise RuntimeError(f"Device {udid} not found on the network")

    def _remembered_device_info(self, udid: str) -> dict | None:
        normalized_udid = str(udid).split("._", 1)[0]
        for device in self._paired_devices.get("devices", []):
            identifiers = {str(i).split("._", 1)[0] for i in device.get("identifiers", [])}
            if normalized_udid not in identifiers:
                continue
            model = device.get("model", "")
            return {
                "name": device.get("name") or "Apple TV",
                "model": model,
                "udid": udid,
                "host": device.get("host", ""),
                "port": 49152,
                "service": "_remotepairing._tcp.local.",
                "device_class": device_class_for(name=device.get("name", ""), model=model),
                "connection": "network",
                "installable": False,
                "needs_setup": False,
                "paired": True,
                "requires_tunnel": True,
                "tunnel_active": bool(self._tunnel_address and self._tunnel_port),
                "properties": {},
            }
        return None

    def _load_paired_devices(self) -> dict:
        try:
            with PAIRED_DEVICES_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    "hosts": list({str(h) for h in data.get("hosts", []) if h}),
                    "identifiers": list({str(i) for i in data.get("identifiers", []) if i}),
                    "devices": data.get("devices", []),
                }
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Failed to load paired device state")
        return {"hosts": [], "identifiers": [], "devices": []}

    def _save_paired_devices(self):
        try:
            STATE_DIR.mkdir(exist_ok=True)
            with PAIRED_DEVICES_PATH.open("w", encoding="utf-8") as f:
                json.dump(self._paired_devices, f, indent=2, sort_keys=True)
        except Exception:
            logger.exception("Failed to save paired device state")

    def _remote_paired_identifiers(self) -> set[str]:
        try:
            from pymobiledevice3.remote.tunnel_service import iter_remote_paired_identifiers
            return {str(identifier) for identifier in iter_remote_paired_identifiers()}
        except Exception:
            logger.debug("Could not read pymobiledevice3 remote pairing records", exc_info=True)
            return set()

    def _device_identifiers(self, device: dict) -> set[str]:
        props = device.get("properties", {}) or {}
        values = {
            device.get("udid", ""),
            props.get("identifier", ""),
            props.get("UniqueDeviceID", ""),
            props.get("rpMRtID", ""),
            props.get("deviceid", ""),
        }
        identifiers: set[str] = set()
        for value in values:
            if not value:
                continue
            text = str(value)
            identifiers.add(text)
            if "._" in text:
                identifiers.add(text.split("._", 1)[0])
        return {identifier for identifier in identifiers if identifier}

    def _is_known_paired(self, device: dict, remote_pair_ids: set[str] | None = None) -> bool:
        service = device.get("service", "")
        host = device.get("host", "")
        paired_hosts = set(self._paired_devices.get("hosts", []))
        paired_ids = set(self._paired_devices.get("identifiers", []))
        if remote_pair_ids is None:
            remote_pair_ids = self._remote_paired_identifiers()

        if host and host in paired_hosts:
            return True

        identifiers = self._device_identifiers(device)
        if identifiers & (paired_ids | remote_pair_ids):
            return True

        # `_remotepairing._tcp` identifiers can rotate and do not always match
        # the persisted pairing-record filename. If this Mac has any tvOS remote
        # pairing record and only sees an Apple TV over remotepairing, treat it as
        # paired and let the Connect action prove the tunnel can be opened.
        is_tvos = device.get("device_class") == "tvos" or "AppleTV" in str(device.get("model", ""))
        return bool(remote_pair_ids) and is_tvos and "remotepairing" in service

    def _remember_paired_device(
        self,
        *,
        name: str | None = None,
        host: str | None = None,
        identifiers: list[str | None] | None = None,
        model: str | None = None,
    ):
        hosts = set(self._paired_devices.get("hosts", []))
        ids = set(self._paired_devices.get("identifiers", []))
        if host:
            hosts.add(host)
        for identifier in identifiers or []:
            if identifier:
                ids.add(str(identifier).split("._", 1)[0])

        self._paired_devices["hosts"] = sorted(hosts)
        self._paired_devices["identifiers"] = sorted(ids)

        if host or ids:
            devices = [
                d for d in self._paired_devices.get("devices", [])
                if not host or d.get("host") != host
            ]
            devices.append({
                "name": name or "Apple TV",
                "host": host or "",
                "model": model or "",
                "identifiers": sorted(ids),
            })
            self._paired_devices["devices"] = devices[-20:]
        self._save_paired_devices()

    # ── Pairing + Tunnel ──

    _pairing_state: str = "idle"
    _pin_event: threading.Event = threading.Event()
    _pin_value: str = ""

    async def pair_device(
        self,
        device_name: str | None = None,
        device_udid: str | None = None,
        device_host: str | None = None,
    ) -> dict:
        """Pair with a device. Runs in a background thread so input() doesn't block.

        Apple TV only. The protocol below browses `_remotepairing-manual-pairing._tcp`
        and drives the on-screen PIN handshake; iPhone and iPad advertise neither
        and are paired by plugging them in and tapping Trust.
        """
        known = self._cache.get(device_udid or "") or {}
        if known.get("device_class") in {"ios", "ipados"}:
            label = "iPad" if known.get("device_class") == "ipados" else "iPhone"
            return {
                "status": "error",
                "message": (
                    f"{label}s pair over USB, not over the network. Connect it "
                    f"with a cable, unlock it, and tap Trust."
                ),
            }

        try:
            from pymobiledevice3.bonjour import browse_remotepairing_manual_pairing
        except ImportError as e:
            return {"status": "error", "message": f"pymobiledevice3 remote pairing not available: {e}"}

        selected = self._selected_device(device_udid=device_udid, device_host=device_host)
        if selected:
            device_name = device_name or selected.get("name")
            device_host = device_host or selected.get("host")

        logger.info(
            "Browsing for devices to pair with (name=%s, udid=%s, host=%s)...",
            device_name,
            device_udid,
            device_host,
        )
        self._pairing_state = "browsing"
        try:
            answers = await browse_remotepairing_manual_pairing(timeout=10.0)
        except Exception as e:
            self._pairing_state = "error"
            return {"status": "error", "message": f"Device browse failed: {e}"}

        for answer in answers:
            logger.info(
                "Pairable answer: instance=%s host=%s port=%s addrs=%s props=%s",
                answer.instance,
                answer.host,
                answer.port,
                [a.full_ip for a in answer.addresses],
                answer.properties,
            )

        target = self._choose_pairing_target(
            answers,
            selected=selected,
            device_name=device_name,
            device_udid=device_udid,
            device_host=device_host,
        )

        if not target:
            self._pairing_state = "error"
            if selected or device_host:
                return {
                    "status": "error",
                    "message": (
                        f"Catapult can see the Apple TV{f' at {device_host}' if device_host else ''}, "
                        "but it is not advertising manual pairing right now.\n\n"
                        "Keep Apple TV on Settings → Remotes and Devices → Remote App and Devices, "
                        "wait a few seconds, then click Setup again. If it still fails, toggle "
                        "Developer Mode or restart the Apple TV."
                    ),
                }
            return {
                "status": "error",
                "message": (
                    "No pairable Apple TV was found. Open Settings → Remotes and Devices → "
                    "Remote App and Devices on the Apple TV, then click Setup again."
                ),
            }

        ip, port, identifier = target
        logger.info("Pairing with %s at %s:%d", device_name or "device", ip, port)
        self._pairing_state = "pairing"

        # Run in a thread — the pairing calls input() which blocks, and we need
        # the main event loop free to handle the PIN submission HTTP request
        try:
            result = await asyncio.to_thread(self._pair_in_thread, identifier, ip, port)
            if result.get("status") == "ok":
                selected_ids = list(self._device_identifiers(selected or {}))
                self._remember_paired_device(
                    name=device_name,
                    host=device_host or ip,
                    identifiers=[identifier, device_udid, *selected_ids],
                    model=(selected or {}).get("model"),
                )
            return result
        except Exception as e:
            # If _pair_in_thread itself raises (e.g., import failure in the bundled
            # .app), state would otherwise be stranded at "pairing".
            self._pairing_state = "error"
            logger.exception("Pairing thread raised")
            return {"status": "error", "message": f"Pairing failed: {type(e).__name__}: {e}"}

    def _selected_device(self, device_udid: str | None = None, device_host: str | None = None) -> dict | None:
        if device_udid and device_udid in self._cache:
            return self._cache[device_udid]
        if device_host:
            for device in self._cache.values():
                if device.get("host") == device_host:
                    return device
        return None

    def _choose_pairing_target(
        self,
        answers,
        *,
        selected: dict | None,
        device_name: str | None,
        device_udid: str | None,
        device_host: str | None,
    ) -> tuple[str, int, str] | None:
        """Choose the manual-pairing Bonjour answer that best matches the selected device.

        The visible device row comes from `_remotepairing._tcp`, but pairing uses
        `_remotepairing-manual-pairing._tcp`. Their TXT names often differ, so
        exact friendly-name matching makes setup fail even while the TV is on the
        right screen. Prefer host/identifier matches, then use a single answer.
        """
        if not answers:
            return None

        if len(answers) == 1:
            return self._answer_to_target(answers[0])

        selected_host = device_host or (selected or {}).get("host", "")
        selected_identifiers = {
            v for v in [
                device_udid,
                (selected or {}).get("udid"),
                (selected or {}).get("properties", {}).get("identifier"),
                (selected or {}).get("properties", {}).get("rpMRtID"),
                (selected or {}).get("properties", {}).get("deviceid"),
                (selected or {}).get("properties", {}).get("UniqueDeviceID"),
            ] if v
        }
        selected_names = {
            self._clean_pair_name(v) for v in [
                device_name,
                (selected or {}).get("name"),
                (selected or {}).get("properties", {}).get("name"),
                (selected or {}).get("properties", {}).get("deviceName"),
            ] if v
        }

        def _score(answer) -> int:
            score = 0
            answer_hosts = {self._strip_scope(a.full_ip) for a in answer.addresses}
            answer_identifier = (
                answer.properties.get("identifier")
                or answer.properties.get("rpMRtID")
                or answer.properties.get("deviceid")
                or ""
            )
            answer_names = {
                self._clean_pair_name(v) for v in [
                    answer.properties.get("name"),
                    answer.properties.get("deviceName"),
                    answer.instance.split("._", 1)[0] if answer.instance else "",
                ] if v
            }

            if selected_host and self._strip_scope(selected_host) in answer_hosts:
                score += 100
            if answer_identifier and answer_identifier in selected_identifiers:
                score += 80
            if selected_names.intersection(answer_names):
                score += 30
            elif selected_names and answer_names:
                if any(a in b or b in a for a in selected_names for b in answer_names):
                    score += 10
            if any(not a.full_ip.startswith("fe80") for a in answer.addresses):
                score += 5
            return score

        best = max(answers, key=_score)
        if _score(best) <= 0:
            logger.warning("Multiple pairable devices found but none matched selected device")
            return None
        return self._answer_to_target(best)

    def _answer_to_target(self, answer) -> tuple[str, int, str] | None:
        if not answer.addresses or not answer.port:
            return None
        preferred = next((a.full_ip for a in answer.addresses if not a.full_ip.startswith("fe80")), None)
        ip = preferred or answer.addresses[0].full_ip
        identifier = answer.properties.get("identifier", "")
        return (ip, answer.port, identifier)

    @staticmethod
    def _strip_scope(host: str) -> str:
        return (host or "").split("%", 1)[0]

    @staticmethod
    def _clean_pair_name(name: str) -> str:
        name = (name or "").strip().lower()
        # Drop the duplicate-host suffix appended by our UI names, e.g.
        # "Apple Device (92)" -> "apple device".
        if name.endswith(")") and "(" in name:
            name = name.rsplit("(", 1)[0].strip()
        return name

    def _pair_in_thread(self, identifier: str, ip: str, port: int) -> dict:
        """Run the actual pairing in a separate thread with its own event loop."""
        import builtins
        # Import directly from tunnel_service — going through pymobiledevice3.cli
        # pulls in inquirer3 → readchar, whose __init__ calls importlib.metadata.version()
        # at import time. PyInstaller doesn't bundle that metadata, so the CLI path
        # crashes the pair thread with PackageNotFoundError in the .app build.
        from pymobiledevice3.remote.tunnel_service import RemotePairingManualPairingService
        from pymobiledevice3.exceptions import RemotePairingCompletedError

        class _PinPairingService(RemotePairingManualPairingService):
            # Upstream only prompts for the on-screen PIN when the literal string
            # "AppleTV" appears in the handshake model; some tvOS builds report a
            # model that misses that check, so the library silently falls back to
            # the "000000" placeholder, the TV rejects the SRP proof, and pairing
            # blows up with `KeyError: PROOF` deep in _verify_proof. Always prompt
            # (via our monkeypatched web input) when the consent step left it unset.
            async def _request_pair_consent(self):
                consent = await super()._request_pair_consent()
                if not consent.pin:
                    consent = consent._replace(pin=input("Enter PIN: "))
                return consent

        if not self._pairing_lock.acquire(blocking=False):
            return {"status": "error", "message": "Another pairing is already in progress"}

        original_input = builtins.input
        self._pin_event.clear()
        self._pin_value = ""

        def _web_input(prompt=""):
            logger.info("PIN requested — waiting for web UI input")
            self._pairing_state = "waiting_pin"
            self._pin_event.wait(timeout=120)
            logger.info("Got PIN from web UI: %s", self._pin_value)
            return self._pin_value

        loop = asyncio.new_event_loop()
        builtins.input = _web_input
        try:
            async def _do_pair():
                async with _PinPairingService(identifier, ip, port) as service:
                    await service.connect(autopair=True)

            loop.run_until_complete(_do_pair())
            self._pairing_state = "done"
            logger.info("Pairing successful!")
            return {"status": "ok", "message": "Paired successfully"}
        except RemotePairingCompletedError:
            self._pairing_state = "done"
            logger.info("Pairing completed successfully (connection closed by device)")
            return {"status": "ok", "message": "Paired successfully"}
        except (KeyError, AssertionError):
            # The SRP proof step (pymobiledevice3 _verify_proof) raises a bare
            # KeyError(PROOF) / AssertionError when the TV rejects the code —
            # wrong PIN, or the trust relationship expired (Apple drops it after
            # ~7 days). Translate it into something the user can act on.
            self._pairing_state = "error"
            logger.exception("Apple TV rejected pairing (wrong/expired PIN)")
            return {"status": "error", "message": (
                "Apple TV rejected the pairing code — it was wrong, or the previous "
                "pairing expired (Apple drops trust after ~7 days).\n\n"
                "On the Apple TV: Settings → Remotes and Devices → Remote App and "
                "Devices. Leave that screen open, then click Setup again and type the "
                "fresh 4-digit code it shows."
            )}
        except Exception as e:
            self._pairing_state = "error"
            logger.exception("Pairing failed")
            return {"status": "error", "message": f"Pairing failed: {type(e).__name__}: {e}"}
        finally:
            builtins.input = original_input
            loop.close()
            self._pairing_lock.release()

    def submit_pin(self, pin: str):
        """Called from the web UI to provide the PIN shown on the device."""
        self._pin_value = (pin or "").strip()
        self._pin_event.set()
        self._pairing_state = "pairing"

    _tunnel_address: str | None = None
    _tunnel_port: int | None = None
    _tunnel_udid: str | None = None  # Real device UDID from tunneld
    _tunnel_sub_platform: str | None = None  # "tvOS" or None, from RSD peer info
    _tunneld_owned: bool = False  # True once Catapult has verified its managed helper
    TUNNELD_URL = "http://127.0.0.1:49151"

    async def _ensure_tunneld(self) -> bool:
        """Return True for Catapult's managed tunneld helper.

        The helper runs as a root LaunchDaemon so users do not have to enter an
        admin password every time they reconnect an Apple TV tunnel. We still do
        not trust arbitrary processes on port 49151; stale non-daemon tunneld
        processes are replaced during daemon installation.
        """
        if await self._tunneld_api_ready() and (
            self._tunneld_owned
            or await self._launchdaemon_loaded()
            or await self._trusted_root_tunneld_running()
        ):
            self._tunneld_owned = True
            return True
        self._tunneld_owned = False
        return False

    async def _start_tunneld(self) -> dict:
        """Install or start Catapult's privileged tunneld LaunchDaemon."""
        if await self._ensure_tunneld():
            return {"status": "ok", "message": "tunneld already running"}

        logger.info("Installing or restarting Catapult tunneld LaunchDaemon...")
        result = await self._install_tunneld_launchdaemon()
        if result.get("status") != "ok":
            return result

        for _ in range(12):
            if await self._ensure_tunneld():
                return {"status": "ok", "message": "tunneld helper ready"}
            await asyncio.sleep(1)

        return {"status": "error", "message": f"tunneld helper did not become ready. See {TUNNELD_LOG_PATH}"}

    async def _tunneld_api_ready(self) -> bool:
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.TUNNELD_URL, timeout=2)
                return resp.status_code == 200
        except Exception:
            return False

    async def _launchdaemon_loaded(self) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "/bin/launchctl",
            "print",
            f"system/{TUNNELD_DAEMON_LABEL}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
        return proc.returncode == 0

    async def _trusted_root_tunneld_running(self) -> bool:
        """Trust an existing root-owned pymobiledevice3/Catapult tunneld."""
        proc = await asyncio.create_subprocess_exec(
            "/usr/sbin/lsof",
            "-ti",
            "tcp:49151",
            "-sTCP:LISTEN",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        pids = [pid.strip() for pid in stdout.decode().splitlines() if pid.strip()]
        for pid in pids:
            ps = await asyncio.create_subprocess_exec(
                "/bin/ps",
                "-o",
                "user=",
                "-o",
                "command=",
                "-p",
                pid,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ps_out, _ = await ps.communicate()
            row = ps_out.decode(errors="replace").strip()
            if not row:
                continue
            parts = row.split(None, 1)
            user = parts[0]
            command = parts[1] if len(parts) > 1 else ""
            if user == "root" and (
                "pymobiledevice3 remote tunneld" in command
                or "Catapult --tunneld" in command
            ):
                logger.info("Reusing existing trusted tunneld process (pid=%s)", pid)
                return True
        return False

    def _tunneld_program_arguments(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--tunneld"]
        for env_root in (
            os.environ.get("UV_PROJECT_ENVIRONMENT"),
            str(Path.home() / "Library/Application Support/Catapult/BackendEnv"),
        ):
            if not env_root:
                continue
            python = Path(env_root) / "bin" / "python"
            if python.exists():
                return [
                    str(python),
                    "-m",
                    "pymobiledevice3",
                    "remote",
                    "tunneld",
                    "--no-usb",
                    "--no-usbmux",
                    "--no-mobdev2",
                ]
        return [
            sys.executable,
            "-m",
            "pymobiledevice3",
            "remote",
            "tunneld",
            "--no-usb",
            "--no-usbmux",
            "--no-mobdev2",
        ]

    async def _install_tunneld_launchdaemon(self) -> dict:
        import os
        import plistlib
        import shlex
        import stat
        import tempfile

        plist_data = {
            "Label": TUNNELD_DAEMON_LABEL,
            "ProgramArguments": self._tunneld_program_arguments(),
            "RunAtLoad": True,
            "KeepAlive": True,
            "EnvironmentVariables": {
                "HOME": str(Path.home()),
                "PYTHONUNBUFFERED": "1",
            },
            "StandardOutPath": TUNNELD_LOG_PATH,
            "StandardErrorPath": TUNNELD_LOG_PATH,
        }

        plist_file = tempfile.NamedTemporaryFile(mode="wb", suffix=".plist", delete=False, prefix="catapult_tunneld_")
        try:
            plistlib.dump(plist_data, plist_file)
            plist_file.close()

            plist_src = shlex.quote(plist_file.name)
            plist_dst = shlex.quote(str(TUNNELD_DAEMON_PLIST))
            label = shlex.quote(TUNNELD_DAEMON_LABEL)
            script_body = f"""#!/bin/bash
set -e
/bin/launchctl bootout system {plist_dst} 2>/dev/null || /bin/launchctl bootout system/{label} 2>/dev/null || true
/usr/bin/pkill -9 -f 'Catapult --tunneld' 2>/dev/null || true
/usr/bin/pkill -9 -f 'pymobiledevice3 remote tunneld' 2>/dev/null || true
PIDS=$(/usr/sbin/lsof -ti tcp:49151 2>/dev/null || true)
if [ -n "$PIDS" ]; then /bin/kill -9 $PIDS 2>/dev/null || true; fi
for i in 1 2 3 4 5; do /usr/sbin/lsof -ti tcp:49151 >/dev/null 2>&1 || break; /bin/sleep 1; done
/bin/cp {plist_src} {plist_dst}
/usr/sbin/chown root:wheel {plist_dst}
/bin/chmod 644 {plist_dst}
/bin/launchctl bootstrap system {plist_dst}
/bin/launchctl enable system/{label}
/bin/launchctl kickstart -k system/{label}
echo installed
"""

            script = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="catapult_tunneld_install_")
            try:
                script.write(script_body)
                script.close()
                os.chmod(script.name, stat.S_IRWXU)

                escaped_script = script.name.replace('"', '\\"')
                proc = await asyncio.create_subprocess_exec(
                    "osascript",
                    "-e",
                    f'do shell script "{escaped_script}" with administrator privileges',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                output = stdout.decode().strip()
                err = stderr.decode().strip()
                logger.info("tunneld LaunchDaemon install output (rc=%d): %s %s", proc.returncode, output[:200], err[:200])
                if proc.returncode != 0:
                    return {"status": "error", "message": f"tunneld helper install failed: {(output or err)[:300]}"}
                self._tunneld_owned = True
                return {"status": "ok", "message": "tunneld helper installed"}
            finally:
                try:
                    os.unlink(script.name)
                except Exception:
                    pass
        finally:
            try:
                os.unlink(plist_file.name)
            except Exception:
                pass

    async def start_tunnel(
        self,
        device_udid: str | None = None,
        device_host: str | None = None,
    ) -> dict:
        """Ensure tunneld is running, then wait for a tunnel to appear."""
        if self._tunnel_lock is None:
            self._tunnel_lock = asyncio.Lock()

        async with self._tunnel_lock:
            return await self._start_tunnel_inner(device_udid=device_udid, device_host=device_host)

    def _tunnel_target(self, device_udid: str | None = None, device_host: str | None = None) -> dict | None:
        if device_udid or device_host:
            selected = self._selected_device(device_udid=device_udid, device_host=device_host)
            if selected:
                return selected
            if device_udid:
                remembered = self._remembered_device_info(device_udid)
                if remembered:
                    return remembered
            if device_host:
                for remembered in self._paired_devices.get("devices", []):
                    if remembered.get("host") == device_host:
                        return {
                            "name": remembered.get("name") or "Apple TV",
                            "model": remembered.get("model", ""),
                            "udid": device_udid or "",
                            "host": device_host,
                            "service": "_remotepairing._tcp.local.",
                            "device_class": device_class_for(
                                name=remembered.get("name", ""),
                                model=remembered.get("model", ""),
                            ),
                            "properties": {},
                        }
        return None

    def _target_identifiers(self, target: dict | None) -> set[str]:
        if not target:
            return set()
        identifiers = self._device_identifiers(target)
        target_host = target.get("host", "")
        for remembered in self._paired_devices.get("devices", []):
            if target_host and remembered.get("host") == target_host:
                for identifier in remembered.get("identifiers", []):
                    if identifier:
                        identifiers.add(str(identifier).split("._", 1)[0])
        return {identifier for identifier in identifiers if identifier}

    def _rsd_identifiers(self, rsd) -> set[str]:
        peer_info = getattr(rsd, "peer_info", {}) or {}
        props = peer_info.get("Properties", {}) or {}
        values = {
            getattr(rsd, "udid", ""),
            props.get("UniqueDeviceID", ""),
            props.get("SerialNumber", ""),
            props.get("UniqueChipID", ""),
        }
        return {str(value).split("._", 1)[0] for value in values if value}

    def _rsd_names(self, rsd) -> set[str]:
        peer_info = getattr(rsd, "peer_info", {}) or {}
        props = peer_info.get("Properties", {}) or {}
        all_values = getattr(rsd, "all_values", {}) or {}
        names = {
            props.get("DeviceName", ""),
            props.get("ComputerName", ""),
            props.get("ProductName", ""),
            all_values.get("DeviceName", ""),
            all_values.get("ProductName", ""),
        }
        return {self._clean_pair_name(str(name)) for name in names if name}

    def _score_rsd_for_target(self, rsd, target: dict | None) -> int:
        if not target:
            return 1

        score = 0
        target_ids = self._target_identifiers(target)
        if target_ids & self._rsd_identifiers(rsd):
            score += 100

        target_names = {
            self._clean_pair_name(value)
            for value in [
                target.get("name", ""),
                target.get("properties", {}).get("deviceName", ""),
                target.get("properties", {}).get("name", ""),
            ]
            if value
        }
        rsd_names = self._rsd_names(rsd)
        if target_names and rsd_names:
            if target_names & rsd_names:
                score += 40
            elif any(a in b or b in a for a in target_names for b in rsd_names):
                score += 15

        target_model = str(target.get("model", ""))
        rsd_product = str(getattr(rsd, "product_type", "") or (getattr(rsd, "peer_info", {}) or {}).get("Properties", {}).get("ProductType", ""))
        if target_model and rsd_product and (target_model in rsd_product or rsd_product in target_model):
            score += 20

        return score

    @staticmethod
    def _rsd_device_class(rsd) -> str:
        product = str(
            getattr(rsd, "product_type", "")
            or (getattr(rsd, "peer_info", {}) or {}).get("Properties", {}).get("ProductType", "")
        )
        return device_class_for(name="", model=product)

    def _select_rsd(self, rsds: list, target: dict | None, *, allow_single_fallback: bool) -> object | None:
        if not rsds:
            return None
        if not target:
            return rsds[0]
        scored = [(self._score_rsd_for_target(rsd, target), rsd) for rsd in rsds]
        best_score, best = max(scored, key=lambda item: item[0])
        if best_score > 0:
            return best
        if allow_single_fallback and len(rsds) == 1:
            # The fallback exists because Apple TV mDNS identifiers rotate and
            # often match nothing. But an iPad scores 0 against an Apple TV
            # tunnel for exactly the same reason, and taking "the only tunnel we
            # have" would install the app onto the Apple TV. Only fall back when
            # the classes are compatible.
            target_class = str(target.get("device_class", "")) or "unknown"
            sole_class = self._rsd_device_class(rsds[0])
            if target_class != "unknown" and sole_class != "unknown" and target_class != sole_class:
                logger.warning(
                    "Refusing to use the sole %s tunnel for a %s target (%s)",
                    sole_class,
                    target_class,
                    target.get("name") or target.get("udid") or target.get("host"),
                )
                return None
            logger.info(
                "Using sole tunneled RSD (udid=%s) for selected target %s",
                getattr(rsds[0], "udid", "?"),
                target.get("name") or target.get("udid") or target.get("host"),
            )
            return rsds[0]
        logger.warning(
            "No active RSD tunnel matched selected target %s. Available RSDs: %s",
            target.get("name") or target.get("udid") or target.get("host"),
            [getattr(rsd, "udid", "?") for rsd in rsds],
        )
        return None

    def _cache_active_tunnel(self, rsd, target: dict | None = None):
        self._tunnel_address, self._tunnel_port = getattr(rsd, "service").address
        self._tunnel_udid = getattr(rsd, "udid", None)
        peer_info = getattr(rsd, "peer_info", {}) or {}
        product_type = (peer_info.get("Properties", {}) or {}).get("ProductType", "")
        self._tunnel_sub_platform = "tvOS" if "appletv" in product_type.lower() else None
        if target and target.get("host"):
            self._tunneled_hosts.add(target["host"])
        logger.info(
            "Tunnel ready: %s:%d (device UDID=%s)",
            self._tunnel_address,
            self._tunnel_port,
            self._tunnel_udid,
        )

    async def _poll_for_tunnel(self, target: dict | None, attempts: int = 30) -> bool:
        """Poll tunneld for a matching RSD, caching it on success.

        tunneld auto-discovers Wi-Fi devices, so an empty result during the first
        few polls is normal while it warms up. Returns True once a tunnel for
        ``target`` is active.
        """
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        for i in range(attempts):
            rsds = []
            try:
                rsds = await get_tunneld_devices()
                logger.info("tunneld RSDs: %s", [getattr(rsd, "udid", "?") for rsd in rsds])
                selected = self._select_rsd(rsds, target, allow_single_fallback=True)
                if selected:
                    self._cache_active_tunnel(selected, target)
                    return True
            except Exception as e:
                logger.debug("tunneld poll %d: %s", i, e)
            finally:
                for rsd in rsds:
                    try:
                        await rsd.close()
                    except Exception:
                        pass
            await asyncio.sleep(2)
        return False

    async def _tunnel_listed(self, address: str, port: int) -> bool:
        """Check tunneld's HTTP listing for a tunnel without dialing the device.

        Verifying via get_tunneld_devices() opens an RSD connection to the
        device and closes it again; the device transiently refuses a reconnect
        right after a close, so back-to-back verifications misreported live
        tunnels as stale — and every false negative spun up a duplicate tunnel
        that killed the previous one. The listing is authoritative for what
        tunneld maintains and touches nothing on-device.
        """
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.TUNNELD_URL, timeout=3)
                if resp.status_code != 200:
                    return False
                for tunnels in (resp.json() or {}).values():
                    for tunnel in tunnels:
                        if (
                            tunnel.get("tunnel-address") == address
                            and tunnel.get("tunnel-port") == port
                        ):
                            return True
        except Exception as e:
            logger.debug("tunneld listing check failed: %s", e)
        return False

    async def _request_tunnel_start(self, target: dict | None) -> bool:
        """Explicitly ask tunneld to establish a Wi-Fi tunnel, newest record first.

        tunneld's ambient Wi-Fi monitor retries every stored pairing record
        against every advertised address each cycle; once stale records for a
        re-paired device pile up, that crawl can take minutes or wedge outright,
        leaving the UI stuck on "Connecting...". /start-tunnel with an explicit
        record skips the crawl entirely, and the newest record is almost always
        the one the device still accepts. Returns True once a tunnel matching
        ``target`` is up and cached.
        """
        import httpx
        from pymobiledevice3.pair_records import iter_remote_pair_records

        try:
            records = sorted(
                iter_remote_pair_records(),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except Exception as e:
            logger.debug("Could not list remote pairing records: %s", e)
            return False

        deadline = asyncio.get_running_loop().time() + ACTIVE_TUNNEL_REQUEST_BUDGET_S
        async with httpx.AsyncClient() as client:
            for record in records:
                identifier = record.stem.removeprefix("remote_")
                if asyncio.get_running_loop().time() >= deadline:
                    logger.info("Active tunnel request sweep hit its time budget")
                    break
                try:
                    resp = await client.get(
                        f"{self.TUNNELD_URL}/start-tunnel",
                        params={"udid": identifier, "connection_type": "wifi"},
                        timeout=ACTIVE_TUNNEL_REQUEST_TIMEOUT_S,
                    )
                except Exception as e:
                    logger.debug("start-tunnel request for %s failed: %s", identifier, e)
                    continue
                if resp.status_code != 200:
                    logger.debug("start-tunnel for %s: HTTP %d", identifier, resp.status_code)
                    continue
                logger.info("tunneld established tunnel using pairing record %s", identifier)
                # The tunnel may belong to a different device than ``target``
                # (multiple paired devices) — only stop once the right one is up.
                if await self._poll_for_tunnel(target, attempts=1):
                    return True
        return False

    async def _tunneld_daemon_pid(self) -> int | None:
        """PID of the tunneld LaunchDaemon, read from launchctl.

        The daemon runs as root so its listening socket is invisible to lsof run
        as the user; launchctl can read the system domain and reports the pid.
        """
        proc = await asyncio.create_subprocess_exec(
            "/bin/launchctl",
            "print",
            f"system/{TUNNELD_DAEMON_LABEL}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("pid ="):
                val = line.split("=", 1)[1].strip()
                if val.isdigit():
                    return int(val)
        return None

    async def _tunneld_process_uptime(self) -> float | None:
        """Elapsed seconds since the tunneld daemon process started, or None."""
        pid = await self._tunneld_daemon_pid()
        if pid is None:
            return None
        ps = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-o",
            "etime=",
            "-p",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await ps.communicate()
        return _parse_ps_etime(out.decode().strip())

    async def _restart_stale_tunneld(self) -> dict:
        """Force-reinstall and kickstart the tunneld daemon.

        A long-lived tunneld can wedge — reachable on its HTTP port but no longer
        discovering devices, so every poll returns an empty tunnel map.
        Reinstalling boots out the stale process and kickstarts a fresh one
        (prompting once for admin, same as first-time setup).
        """
        logger.warning("tunneld reachable but no tunnel established; forcing daemon restart")
        self._tunneld_owned = False
        return await self._install_tunneld_launchdaemon()

    async def _start_tunnel_inner(
        self,
        device_udid: str | None = None,
        device_host: str | None = None,
    ) -> dict:
        target = self._tunnel_target(device_udid=device_udid, device_host=device_host)

        if self._tunnel_address and self._tunnel_port:
            if await self._tunnel_listed(self._tunnel_address, self._tunnel_port):
                return {"status": "ok", "message": "Tunnel already active"}

            logger.info("Cached tunnel is stale; clearing and waiting for a live tunnel")
            self._tunnel_address = None
            self._tunnel_port = None
            self._tunnel_udid = None
            self._tunnel_sub_platform = None

        # Start tunneld if not running
        freshly_started = False
        if not await self._ensure_tunneld():
            result = await self._start_tunneld()
            if result["status"] != "ok":
                return result
            freshly_started = True
            # Wait for tunneld to be ready
            await asyncio.sleep(2)

        # A healthy daemon may already have the tunnel up — check briefly before
        # doing anything heavier.
        if await self._poll_for_tunnel(target, attempts=2):
            message = f"Tunnel active at {self._tunnel_address}:{self._tunnel_port}"
            return {"status": "ok", "message": message}

        # Don't just wait for tunneld's ambient discovery to find the device —
        # ask it explicitly, record by record. This is what actually connects in
        # practice; the passive poll below is the fallback.
        logger.info("Requesting tunnel from tunneld with stored pairing records...")
        if await self._request_tunnel_start(target):
            message = f"Tunnel active at {self._tunnel_address}:{self._tunnel_port}"
            return {"status": "ok", "message": message}

        # Poll tunneld for tunnels (it auto-discovers WiFi devices). A daemon we
        # just started is cold and needs the longer window; an adopted one that is
        # healthy usually has the tunnel up already.
        logger.info("Waiting for tunneld to establish tunnel...")
        attempts = TUNNELD_COLD_START_POLL_ATTEMPTS if freshly_started else TUNNELD_POLL_ATTEMPTS
        if await self._poll_for_tunnel(target, attempts=attempts):
            message = f"Tunnel active at {self._tunnel_address}:{self._tunnel_port}"
            return {"status": "ok", "message": message}

        # No tunnel after the poll window. If we adopted an already-running daemon
        # that has been up long enough to be considered wedged (rather than one we
        # just started that is still warming up), force a restart and poll again
        # with the generous cold-start window — a freshly restarted tunneld can
        # take well over a minute to rediscover and re-tunnel the device. This
        # recovers a tunneld that is reachable on its HTTP port but has silently
        # stopped discovering devices, without the user having to restart it by hand.
        if not freshly_started:
            uptime = await self._tunneld_process_uptime()
            if uptime is None or uptime >= STALE_TUNNELD_UPTIME_S:
                restart = await self._restart_stale_tunneld()
                if restart.get("status") == "ok":
                    await asyncio.sleep(2)
                    if await self._poll_for_tunnel(target, attempts=TUNNELD_COLD_START_POLL_ATTEMPTS):
                        message = f"Tunnel active at {self._tunnel_address}:{self._tunnel_port}"
                        return {"status": "ok", "message": message}

        self._tunnel_address = None
        self._tunnel_port = None
        self._tunnel_udid = None
        if target:
            name = target.get("name") or target.get("host") or target.get("udid") or "device"
            return {"status": "error", "message": f"Tunnel not established for {name} — device may need re-pairing"}
        return {"status": "error", "message": "Tunnel not established — device may need re-pairing"}

    async def get_real_udid(
        self,
        device_udid: str | None = None,
        device_host: str | None = None,
    ) -> tuple[str, str | None]:
        """Get the real device UDID from RSD and detect platform (tvOS vs iOS).

        Tunnel-only. iPhone and iPad reach installd over classic lockdown via
        usbmux and never come through here — callers short-circuit on
        service == "usbmux".
        """
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        target = self._tunnel_target(device_udid=device_udid, device_host=device_host)
        target_class = str((target or {}).get("device_class", "")) or "unknown"
        if target_class not in TUNNEL_DEVICE_CLASSES and target_class != "unknown":
            # Previously this fell through to start_tunnel(), which fired an
            # admin password prompt and polled for roughly three minutes before
            # failing, for a device that can never be tunneled: tunneld runs
            # with --no-usb --no-usbmux --no-mobdev2.
            raise RuntimeError(
                "Catapult installs to iPhone and iPad over USB. Connect the "
                "device with a cable, unlock it, and tap Trust."
            )

        # Prefer the identity cached when the tunnel came up — dialing the RSD
        # again right after another connection closed can transiently fail and
        # made installs abort with "No active Apple TV tunnel" despite a live one.
        if self._tunnel_udid and self._tunnel_address and self._tunnel_port:
            if await self._tunnel_listed(self._tunnel_address, self._tunnel_port):
                logger.info(
                    "Real device UDID (cached): %s, subPlatform: %s",
                    self._tunnel_udid,
                    self._tunnel_sub_platform,
                )
                return (self._tunnel_udid, self._tunnel_sub_platform)

        rsds = await get_tunneld_devices()
        if not rsds:
            tunnel = await self.start_tunnel(device_udid=device_udid, device_host=device_host)
            if tunnel.get("status") == "ok":
                rsds = await get_tunneld_devices()

        if not rsds:
            raise RuntimeError("No active Apple TV tunnel. Use Setup when the Apple TV is awake and on this network.")

        rsd = self._select_rsd(rsds, target, allow_single_fallback=True)
        if not rsd:
            for candidate in rsds:
                try:
                    await candidate.close()
                except Exception:
                    pass
            raise RuntimeError("No active tunnel matched the selected Apple TV. Reconnect it from Setup.")

        udid = rsd.udid
        # Detect Apple TV via product type or name in peer info
        peer_info = getattr(rsd, "peer_info", {}) or {}
        props = peer_info.get("Properties", {})
        product_type = props.get("ProductType", "")
        is_tv = "AppleTV" in product_type or "appletv" in product_type.lower()
        sub_platform = "tvOS" if is_tv else None
        logger.info("Real device UDID: %s, ProductType: %s, subPlatform: %s",
                    udid, product_type, sub_platform)
        for candidate in rsds:
            try:
                await candidate.close()
            except Exception:
                pass
        return (udid, sub_platform)

    async def find_installed_app(
        self,
        *,
        bundle_id: str,
        display_name: str,
        candidate_bundle_ids: list[str],
        team_id: str,
        device_udid: str | None = None,
    ) -> dict | None:
        """Find an already-installed app that should be updated in place."""
        apps = await self.list_installed_apps(device_udid=device_udid)
        candidate_set = {value for value in candidate_bundle_ids if value}
        normalized_name = display_name.strip().casefold()
        catapult_prefix = f"com.catapult.{team_id}."

        exact = next((app for app in apps if app.get("bundle_id") == bundle_id), None)
        if exact:
            return exact

        candidate = next((app for app in apps if app.get("bundle_id") in candidate_set), None)
        if candidate:
            return candidate

        if normalized_name:
            matches = [
                app for app in apps
                if app.get("bundle_id", "").startswith(catapult_prefix)
                and app.get("name", "").strip().casefold() == normalized_name
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                logger.warning(
                    "Multiple installed Catapult apps match %s: %s",
                    display_name,
                    [app.get("bundle_id") for app in matches],
                )
        return None

    async def list_installed_apps(self, device_udid: str | None = None) -> list[dict]:
        """List installed user apps through the active RSD tunnel."""
        from pymobiledevice3.tunneld.api import get_tunneld_devices
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        target = None
        if device_udid:
            device = self._cache.get(device_udid) or self._remembered_device_info(device_udid)
            if device and device.get("service") == "usbmux":
                return await self._list_installed_apps_usbmux(device_udid)
            target = device

        rsds = await get_tunneld_devices()
        if not rsds:
            tunnel = await self.start_tunnel(device_udid=device_udid, device_host=(target or {}).get("host"))
            if tunnel.get("status") == "ok":
                rsds = await get_tunneld_devices()
        if not rsds:
            logger.info("No active RSD tunnel while listing installed apps")
            return []
        rsd = self._select_rsd(rsds, target, allow_single_fallback=True)
        if not rsd:
            logger.info("No matching RSD tunnel while listing installed apps for %s", device_udid or "default target")
            for candidate in rsds:
                try:
                    await candidate.close()
                except Exception:
                    pass
            return []
        try:
            installer = InstallationProxyService(lockdown=rsd)
            attributes = [
                "CFBundleIdentifier",
                "CFBundleDisplayName",
                "CFBundleName",
                "CFBundleShortVersionString",
                "ApplicationType",
            ]
            rows = await installer.browse(attributes=attributes)
            apps = []
            for row in rows:
                if row.get("ApplicationType") != "User":
                    continue
                apps.append({
                    "bundle_id": row.get("CFBundleIdentifier", ""),
                    "name": row.get("CFBundleDisplayName") or row.get("CFBundleName") or "",
                    "version": row.get("CFBundleShortVersionString", ""),
                })
            return apps
        finally:
            for rsd in rsds:
                try:
                    await rsd.close()
                except Exception:
                    pass

    async def _list_installed_apps_usbmux(self, udid: str) -> list[dict]:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        lockdown = await create_using_usbmux(serial=udid, pair_timeout=3)
        installer = InstallationProxyService(lockdown=lockdown)
        attributes = [
            "CFBundleIdentifier",
            "CFBundleDisplayName",
            "CFBundleName",
            "CFBundleShortVersionString",
            "ApplicationType",
        ]
        rows = await installer.browse(attributes=attributes)
        apps = []
        for row in rows:
            if row.get("ApplicationType") != "User":
                continue
            apps.append({
                "bundle_id": row.get("CFBundleIdentifier", ""),
                "name": row.get("CFBundleDisplayName") or row.get("CFBundleName") or "",
                "version": row.get("CFBundleShortVersionString", ""),
            })
        return apps

    # ── Installation ──

    async def install(self, udid: str, ipa_path: Path):
        device = await self.get_device_info(udid)
        host = device["host"]
        port = device.get("port", 62078)
        service = device.get("service", "")
        installable = device.get("installable", False)

        # tvOS / remotepairing devices install through the tunnel. The cached
        # `installable` flag comes from the last scan and lags the tunnel coming
        # up (~20-30s), so an install fired right after Setup can race ahead of
        # the tunnel and fail spuriously with "not ready". Ensure (and wait for)
        # the tunnel here instead of trusting the stale flag — start_tunnel() is
        # idempotent and returns immediately if the tunnel is already active, and
        # won't re-prompt for admin when tunneld is already running.
        if "remotepairing" in service and not installable:
            logger.info("Tunnel not marked ready for %s — ensuring before install...", device["name"])
            tunnel = await self.start_tunnel(device_udid=udid, device_host=host)
            if tunnel.get("status") == "ok":
                installable = True

        if not installable:
            if device.get("device_class") in {"ios", "ipados"}:
                raise RuntimeError(
                    f"'{device['name']}' was found on the network, but Catapult "
                    f"installs to iPhone and iPad over USB. Connect it with a "
                    f"cable, unlock it, and tap Trust."
                )
            raise RuntimeError(
                f"Device '{device['name']}' is not ready for installation. "
                f"Use the 'Setup' button to pair and create a tunnel first."
            )

        logger.info("Installing to %s (%s:%s via %s)", device["name"], host, port, service)

        if service == "usbmux" or device.get("connection") == "usb":
            await self._install_via_usbmux(udid, ipa_path)
            logger.info("Installation complete")
            return

        # Prefer a live RSD tunnel matching this UDID (required for tvOS;
        # also survives LaunchAgent restarts since tunneld runs separately).
        # mobdev2 mDNS records don't carry the real UDID, so exact match often
        # fails — when that happens but exactly one tunnel exists, use it.
        from pymobiledevice3.tunneld.api import get_tunneld_devices
        try:
            rsds = await get_tunneld_devices()
        except Exception as e:
            logger.debug("tunneld query failed: %s", e)
            rsds = []
        if not rsds and ("remotepairing" in service or device.get("requires_tunnel")):
            tunnel = await self.start_tunnel(device_udid=udid, device_host=host)
            if tunnel.get("status") == "ok":
                try:
                    rsds = await get_tunneld_devices()
                except Exception as e:
                    logger.debug("tunneld query after start failed: %s", e)
                    rsds = []

        logger.info("tunneld RSDs: %s (target udid=%s)",
                    [getattr(r, "udid", "?") for r in rsds], udid)
        matching = self._select_rsd(rsds, device, allow_single_fallback=True)
        if matching is not None:
            try:
                await self._install_via_rsd_client(matching, ipa_path)
            finally:
                for r in rsds:
                    await r.close()
        else:
            for r in rsds:
                await r.close()
            if "remotepairing" in service:
                raise RuntimeError("No active tunnel. Use Setup to pair and create a tunnel first.")
            elif "mobdev2" in service:
                await self._install_via_lockdown(host, port, udid, ipa_path)
            else:
                raise RuntimeError(f"No supported installation method for service {service}")

        logger.info("Installation complete")

    async def _install_via_rsd_client(self, rsd, ipa_path: Path):
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        installer = InstallationProxyService(lockdown=rsd)
        await installer.install_from_local(str(ipa_path))

    async def _install_via_lockdown(self, host: str, port: int, udid: str, ipa_path: Path):
        from pymobiledevice3.lockdown import create_using_tcp
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        # autopair=False deliberately. TcpLockdownClient.__init__ overwrites
        # identifier with the hostname (pymobiledevice3 lockdown.py:1499-1500),
        # so the pair-record lookup searches for a record named after the IP,
        # finds none, and the default autopair=True pops an interactive Trust
        # dialog — from inside the background refresh loop. Failing with a clear
        # error beats blocking on a prompt nobody is there to answer.
        lockdown = await create_using_tcp(
            host, identifier=udid, port=port, autopair=False
        )
        installer = InstallationProxyService(lockdown=lockdown)
        await installer.install_from_local(str(ipa_path))

    async def _install_via_usbmux(self, udid: str, ipa_path: Path):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        lockdown = await create_using_usbmux(serial=udid, pair_timeout=3)
        installer = InstallationProxyService(lockdown=lockdown)
        await installer.install_from_local(str(ipa_path))
