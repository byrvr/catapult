"""Network device discovery, pairing, tunnel, and app installation."""

import asyncio
import logging
import sys
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

INSTALLABLE_SERVICES = {"_remotepairing._tcp.local.", "_apple-mobdev2._tcp.local."}


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

        device_name = (props.get("model", "") or props.get("rpMd", "")
                       or props.get("deviceName", "") or name.split(".")[0])
        udid = (props.get("UniqueDeviceID") or props.get("rpMRtID")
                or props.get("deviceid") or name)
        device_class = "unknown"
        for prefix, cls in DEVICE_CLASS_MAP.items():
            if prefix.lower() in device_name.lower():
                device_class = cls
                break

        key = f"{addresses[0]}:{info.port}:{stype}"
        self.found[key] = {
            "name": device_name,
            "udid": udid,
            "host": addresses[0],
            "port": info.port,
            "service": stype,
            "device_class": device_class,
            "connection": "network",
            "installable": stype in INSTALLABLE_SERVICES,
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

        # Deduplicate by host — prefer installable over AirPlay
        by_host: dict[str, dict] = {}
        for d in raw:
            host = d["host"]
            if host not in by_host or (d["installable"] and not by_host[host]["installable"]):
                by_host[host] = d

        devices = list(by_host.values())
        for d in devices:
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

    async def pair_device(self) -> dict:
        """Pair with a device using pymobiledevice3. Requires admin privileges.

        Uses osascript to show macOS admin password dialog for sudo access.
        The pairing process shows a PIN on the target device.
        """
        python = sys.executable
        pair_cmd = f'"{python}" -m pymobiledevice3 remote pair'

        logger.info("Initiating device pairing (admin privileges required)...")
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e",
            f'do shell script "{pair_cmd}" with administrator privileges',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        output = stdout.decode() + stderr.decode()
        logger.info("Pair result (rc=%d): %s", proc.returncode, output[:300])

        if proc.returncode != 0:
            return {"status": "error", "message": f"Pairing failed: {output[:200]}"}
        return {"status": "ok", "message": "Paired successfully"}

    async def start_tunnel(self) -> dict:
        """Start a tunnel to paired devices. Runs in background with admin privileges."""
        if self._tunnel_proc and self._tunnel_proc.returncode is None:
            return {"status": "ok", "message": "Tunnel already running"}

        python = sys.executable
        tunnel_cmd = f'"{python}" -m pymobiledevice3 remote start-tunnel'

        logger.info("Starting device tunnel (admin privileges required)...")
        # Use script so tunnel stays running after osascript returns
        script = f'''
        do shell script "{tunnel_cmd} &> /tmp/catapult_tunnel.log & echo $!" with administrator privileges
        '''
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script.strip(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip()
        logger.info("Tunnel started (pid=%s)", output)

        if proc.returncode != 0:
            err = stderr.decode()
            return {"status": "error", "message": f"Tunnel failed: {err[:200]}"}

        # Wait a moment for tunnel to establish, then rescan
        await asyncio.sleep(3)
        return {"status": "ok", "message": "Tunnel started"}

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

        if "remotepairing" in service:
            await self._install_via_rsd(host, port, ipa_path)
        elif "mobdev2" in service:
            await self._install_via_lockdown(host, port, ipa_path)
        else:
            raise RuntimeError(f"No supported installation method for service {service}")

        logger.info("Installation complete")

    async def _install_via_rsd(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.lockdown import create_using_remote
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        rsd = RemoteServiceDiscoveryService((host, port))
        await rsd.connect()
        try:
            lockdown = create_using_remote(rsd)
            installer = InstallationProxyService(lockdown=lockdown)
            await installer.install_from_local(str(ipa_path))
        finally:
            rsd.close()

    async def _install_via_lockdown(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.lockdown import create_using_tcp
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        lockdown = create_using_tcp(host, port)
        installer = InstallationProxyService(lockdown=lockdown)
        await installer.install_from_local(str(ipa_path))
