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
        device_name = friendly_name or props.get("deviceName", "") or model or name.split(".")[0]
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

    async def discover(self, timeout: float = 4.0) -> list[dict]:
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

        devices = list(by_host.values())
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
        result = await asyncio.to_thread(self._pair_in_thread, identifier, ip, port)
        return result

    def _pair_in_thread(self, identifier: str, ip: str, port: int) -> dict:
        """Run the actual pairing in a separate thread with its own event loop."""
        import builtins
        from pymobiledevice3.cli.remote import RemotePairingManualPairingService
        from pymobiledevice3.exceptions import RemotePairingCompletedError

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
                async with RemotePairingManualPairingService(identifier, ip, port) as service:
                    await service.connect(autopair=True)

            loop.run_until_complete(_do_pair())
            self._pairing_state = "done"
            logger.info("Pairing successful!")
            return {"status": "ok", "message": "Paired successfully"}
        except RemotePairingCompletedError:
            # This is actually a SUCCESS — pymobiledevice3 raises this when
            # pairing completes and the remote endpoint closes the connection.
            self._pairing_state = "done"
            logger.info("Pairing completed successfully (connection closed by device)")
            return {"status": "ok", "message": "Paired successfully"}
        except Exception as e:
            self._pairing_state = "error"
            logger.exception("Pairing failed")
            return {"status": "error", "message": f"Pairing failed: {type(e).__name__}: {e}"}
        finally:
            builtins.input = original_input
            loop.close()

    def submit_pin(self, pin: str):
        """Called from the web UI to provide the PIN shown on the device."""
        self._pin_value = pin
        self._pin_event.set()
        self._pairing_state = "pairing"

    _tunnel_address: str | None = None
    _tunnel_port: int | None = None
    _tunnel_udid: str | None = None  # Real device UDID from tunneld
    TUNNELD_URL = "http://127.0.0.1:49151"

    async def _ensure_tunneld(self) -> bool:
        """Check if tunneld is running, start it if not."""
        import httpx
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self.TUNNELD_URL, timeout=2)
                return resp.status_code == 200
        except Exception:
            return False

    async def _start_tunneld(self) -> dict:
        """Start tunneld as a background daemon with admin privileges."""
        import os, tempfile, stat

        home = Path.home()
        command = (
            f"pkill -f 'pymobiledevice3 remote tunneld' 2>/dev/null; sleep 1; "
            f"HOME={home} PYTHONUNBUFFERED=1 "
            f"{sys.executable} -m pymobiledevice3 remote tunneld --no-usb --no-usbmux --no-mobdev2 "
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
            return {"status": "ok", "message": f"tunneld started (PID {output})"}
        finally:
            os.unlink(script.name)

    async def start_tunnel(self) -> dict:
        """Ensure tunneld is running, then wait for a tunnel to appear."""
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
            for i in range(15):
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

    # ── Installation ──

    async def install(self, udid: str, ipa_path: Path):
        device = await self.get_device_info(udid)
        host = device["host"]
        port = device.get("port", 62078)
        service = device.get("service", "")
        installable = device.get("installable", False)

        if not installable:
            raise RuntimeError(
                f"Device '{device['name']}' is not ready for installation. "
                f"Use the 'Setup' button to pair and create a tunnel first."
            )

        logger.info("Installing to %s (%s:%s via %s)", device["name"], host, port, service)

        if "remotepairing" in service or self._tunnel_address:
            # Use tunnel RSD address instead of direct device IP
            if not self._tunnel_address:
                raise RuntimeError("No active tunnel. Use Setup to pair and create a tunnel first.")
            logger.info("Using tunnel at %s:%d", self._tunnel_address, self._tunnel_port)
            await self._install_via_rsd(self._tunnel_address, self._tunnel_port, ipa_path)
        elif "mobdev2" in service:
            await self._install_via_lockdown(host, port, ipa_path)
        else:
            raise RuntimeError(f"No supported installation method for service {service}")

        logger.info("Installation complete")

    async def _install_via_rsd(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.tunneld.api import get_tunneld_devices
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        rsds = await get_tunneld_devices()
        if not rsds:
            raise RuntimeError("No tunneled devices found. Try Setup again.")
        rsd = rsds[0]
        try:
            installer = InstallationProxyService(lockdown=rsd)
            await installer.install_from_local(str(ipa_path))
        finally:
            await rsd.close()

    async def _install_via_lockdown(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.lockdown import create_using_tcp
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        lockdown = create_using_tcp(host, port)
        installer = InstallationProxyService(lockdown=lockdown)
        await installer.install_from_local(str(ipa_path))
