"""Network device discovery, pairing, tunnel, and app installation."""

import asyncio
import logging
import sys
import threading
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
        device_class = "unknown"
        # Check both name and model for device type
        check_str = f"{device_name} {model}".lower()
        for prefix, cls in DEVICE_CLASS_MAP.items():
            if prefix.lower() in check_str:
                device_class = cls
                break

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
            "installable": stype in INSTALLABLE_SERVICES,
            "needs_setup": stype in NEEDS_SETUP_SERVICES,
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
        self._pairing_lock = threading.Lock()
        self._tunnel_lock: asyncio.Lock | None = None  # created lazily in async context

    async def discover(self, timeout: float = 6.0) -> list[dict]:
        def _scan():
            zc = Zeroconf()
            listener = _Listener()
            for svc in MDNS_SERVICES:
                ServiceBrowser(zc, svc, listener)
            import time
            time.sleep(timeout)
            results = list(listener.found.values())
            zc.close()
            return results

        logger.info("Scanning network for devices (%ss timeout)...", timeout)
        raw = await asyncio.to_thread(_scan)

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
            check_str = f"{d['name']} {d.get('model', '')}".lower()
            for prefix, cls in DEVICE_CLASS_MAP.items():
                if prefix.lower() in check_str:
                    d["device_class"] = cls
                    break

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

        for d in devices:
            # Preserve tunnel state across rescans
            if d["host"] in self._tunneled_hosts:
                d["installable"] = True
                d["needs_setup"] = False
            self._cache[d["udid"]] = d

        logger.info("Found %d device(s)", len(devices))
        return devices

    async def get_device_info(self, udid: str) -> dict:
        if udid in self._cache:
            return self._cache[udid]
        devices = await self.discover()
        for d in devices:
            if d["udid"] == udid:
                return d
        raise RuntimeError(f"Device {udid} not found on the network")

    # ── Pairing + Tunnel ──

    _pairing_state: str = "idle"
    _pin_event: threading.Event = threading.Event()
    _pin_value: str = ""

    async def pair_device(self, device_name: str | None = None) -> dict:
        """Pair with a device. Runs in a background thread so input() doesn't block."""
        try:
            from pymobiledevice3.bonjour import browse_remotepairing_manual_pairing
        except ImportError as e:
            return {"status": "error", "message": f"pymobiledevice3 remote pairing not available: {e}"}

        logger.info("Browsing for devices to pair with (name=%s)...", device_name)
        self._pairing_state = "browsing"
        try:
            answers = await browse_remotepairing_manual_pairing()
        except Exception as e:
            self._pairing_state = "error"
            return {"status": "error", "message": f"Device browse failed: {e}"}

        target = None
        for answer in answers:
            name = answer.properties.get("name", "")
            if device_name and name != device_name:
                continue
            for addr in answer.addresses:
                if not addr.full_ip.startswith("fe80"):
                    target = (addr.full_ip, answer.port, answer.properties.get("identifier", ""))
                    break
            if target:
                break
            if answer.addresses:
                addr = answer.addresses[0]
                target = (addr.full_ip, answer.port, answer.properties.get("identifier", ""))
                break

        if not target:
            self._pairing_state = "error"
            return {"status": "error", "message": "No pairable device found. Enable Developer Mode on Apple TV."}

        ip, port, identifier = target
        logger.info("Pairing with %s at %s:%d", device_name or "device", ip, port)
        self._pairing_state = "pairing"

        # Run in a thread — the pairing calls input() which blocks, and we need
        # the main event loop free to handle the PIN submission HTTP request
        try:
            result = await asyncio.to_thread(self._pair_in_thread, identifier, ip, port)
            return result
        except Exception as e:
            # If _pair_in_thread itself raises (e.g., import failure in the bundled
            # .app), state would otherwise be stranded at "pairing".
            self._pairing_state = "error"
            logger.exception("Pairing thread raised")
            return {"status": "error", "message": f"Pairing failed: {type(e).__name__}: {e}"}

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
    _tunneld_owned: bool = False  # True once WE started tunneld this process
    TUNNELD_URL = "http://127.0.0.1:49151"

    async def _ensure_tunneld(self) -> bool:
        """Return True only if a tunneld WE started this session is responding.

        A foreign/stale tunneld — e.g. a root-owned zombie left over from a
        previous app run — happily answers the HTTP API with 200 but its
        tunnel-building machinery is dead, so it returns an empty tunnel list
        forever ("Tunnel not established"). Trusting it (status==200) meant we
        skipped _start_tunneld() and never cleared it. So unless we started
        this tunneld ourselves, report it as not-ready to force a clean restart.
        """
        import httpx
        if not self._tunneld_owned:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.TUNNELD_URL, timeout=2)
                return resp.status_code == 200
        except Exception:
            self._tunneld_owned = False
            return False

    async def _start_tunneld(self) -> dict:
        """Start tunneld as a background daemon with admin privileges."""
        import os, tempfile, stat

        home = Path.home()
        # In the PyInstaller .app bundle, sys.executable is the Catapult bootloader,
        # not a Python interpreter — `-m pymobiledevice3` won't work. The bundle
        # accepts --tunneld to run pymobiledevice3.tunneld.TunneldRunner in-process.
        # In dev (running from source), sys.executable is the venv python; we use
        # `-m pymobiledevice3 …` so behavior matches the historical command.
        if getattr(sys, "frozen", False):
            tunneld_invocation = f"'{sys.executable}' --tunneld"
        else:
            tunneld_invocation = (
                f"'{sys.executable}' -m pymobiledevice3 remote tunneld "
                f"--no-usb --no-usbmux --no-mobdev2"
            )
        command = (
            # Clear any prior tunneld before starting a new one. The previous
            # single pkill used a regex alternation ('A|B') that macOS pkill does
            # NOT treat as alternation, so a stale (even root-owned) tunneld
            # survived and kept port 49151 bound — the new tunneld then died with
            # [Errno 48] address already in use. Kill by name (separate calls)
            # AND by whoever holds the port, then wait for the socket to release.
            f"pkill -9 -f 'Catapult --tunneld' 2>/dev/null; "
            f"pkill -9 -f 'pymobiledevice3 remote tunneld' 2>/dev/null; "
            f"PIDS=$(lsof -ti tcp:49151 2>/dev/null); [ -n \"$PIDS\" ] && kill -9 $PIDS 2>/dev/null; "
            f"for i in 1 2 3 4 5; do lsof -ti tcp:49151 >/dev/null 2>&1 || break; sleep 1; done; "
            f"HOME={home} PYTHONUNBUFFERED=1 "
            f"{tunneld_invocation} "
            f"> /tmp/catapult_tunneld.log 2>&1 & "
            f"TPID=$!; sleep 3; "
            f"if kill -0 $TPID 2>/dev/null; then echo $TPID; else cat /tmp/catapult_tunneld.log; exit 1; fi"
        )

        script = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="catapult_")
        script.write(f"#!/bin/bash\n{command}\n")
        script.close()
        os.chmod(script.name, stat.S_IRWXU)

        logger.info("Starting tunneld (admin privileges required)...")
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'do shell script "{script.name}" with administrator privileges',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            output = stdout.decode().strip()
            err = stderr.decode().strip()
            logger.info("tunneld output (rc=%d): %s %s", proc.returncode, output[:200], err[:200])

            if proc.returncode != 0:
                return {"status": "error", "message": f"tunneld failed: {(output or err)[:200]}"}
            self._tunneld_owned = True
            return {"status": "ok", "message": f"tunneld started (PID {output})"}
        finally:
            os.unlink(script.name)

    async def start_tunnel(self) -> dict:
        """Ensure tunneld is running, then wait for a tunnel to appear."""
        import httpx

        if self._tunnel_lock is None:
            self._tunnel_lock = asyncio.Lock()

        async with self._tunnel_lock:
            return await self._start_tunnel_inner()

    async def _start_tunnel_inner(self) -> dict:
        import httpx

        # Already have a tunnel
        if self._tunnel_address and self._tunnel_port:
            return {"status": "ok", "message": "Tunnel already active"}

        # Start tunneld if not running
        if not await self._ensure_tunneld():
            result = await self._start_tunneld()
            if result["status"] != "ok":
                return result
            # Wait for tunneld to be ready
            await asyncio.sleep(2)

        # Poll tunneld for tunnels (it auto-discovers WiFi devices)
        logger.info("Waiting for tunneld to establish tunnel...")
        async with httpx.AsyncClient() as client:
            for i in range(30):
                try:
                    resp = await client.get(self.TUNNELD_URL, timeout=3)
                    tunnels = resp.json()
                    logger.info("tunneld tunnels: %s", tunnels)
                    for udid, details in tunnels.items():
                        if details:
                            t = details[0]
                            self._tunnel_address = t["tunnel-address"]
                            self._tunnel_port = t["tunnel-port"]
                            self._tunnel_udid = udid
                            logger.info("Tunnel ready: %s:%d (device UDID=%s)",
                                        self._tunnel_address, self._tunnel_port, udid)
                            return {"status": "ok",
                                    "message": f"Tunnel active at {self._tunnel_address}:{self._tunnel_port}"}
                except Exception as e:
                    logger.debug("tunneld poll %d: %s", i, e)
                await asyncio.sleep(2)

        return {"status": "error", "message": "Tunnel not established — device may need re-pairing"}

    async def get_real_udid(self) -> tuple[str, str | None]:
        """Get the real device UDID from RSD and detect platform (tvOS vs iOS)."""
        from pymobiledevice3.tunneld.api import get_tunneld_devices

        rsds = await get_tunneld_devices()
        if not rsds:
            # Fall back to tunneld key (pairing UUID) if RSD unavailable
            return (self._tunnel_udid or "", None)

        rsd = rsds[0]
        udid = rsd.udid
        # Detect Apple TV via product type or name in peer info
        peer_info = getattr(rsd, "peer_info", {}) or {}
        props = peer_info.get("Properties", {})
        product_type = props.get("ProductType", "")
        is_tv = "AppleTV" in product_type or "appletv" in product_type.lower()
        sub_platform = "tvOS" if is_tv else None
        logger.info("Real device UDID: %s, ProductType: %s, subPlatform: %s",
                    udid, product_type, sub_platform)
        await rsd.close()
        return (udid, sub_platform)

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
            tunnel = await self.start_tunnel()
            if tunnel.get("status") == "ok":
                installable = True

        if not installable:
            raise RuntimeError(
                f"Device '{device['name']}' is not ready for installation. "
                f"Use the 'Setup' button to pair and create a tunnel first."
            )

        logger.info("Installing to %s (%s:%s via %s)", device["name"], host, port, service)

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

        logger.info("tunneld RSDs: %s (target udid=%s)",
                    [getattr(r, "udid", "?") for r in rsds], udid)
        matching = next((r for r in rsds if getattr(r, "udid", None) == udid), None)
        if matching is None and len(rsds) == 1:
            matching = rsds[0]
            logger.info("Using sole tunneled RSD (udid=%s) for target (udid=%s)",
                        matching.udid, udid)
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

        lockdown = await create_using_tcp(host, identifier=udid, port=port)
        installer = InstallationProxyService(lockdown=lockdown)
        await installer.install_from_local(str(ipa_path))
