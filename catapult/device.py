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

    async def pair_device(self, device_name: str | None = None) -> dict:
        """Pair with a device using pymobiledevice3. Requires admin privileges."""
        cmd = f"{sys.executable} -m pymobiledevice3 remote pair"
        if device_name:
            cmd += f' --name "{device_name}"'
        return await self._run_privileged(cmd, "Pairing")

    async def _run_privileged(self, command: str, label: str) -> dict:
        """Run a command with admin privileges via macOS password dialog."""
        import tempfile, os, stat

        # Write to a temp script to avoid AppleScript quoting issues
        script = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, prefix="catapult_")
        script.write(f"#!/bin/bash\n{command}\n")
        script.close()
        os.chmod(script.name, stat.S_IRWXU)

        logger.info("%s: running with admin privileges...", label)
        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e",
                f'do shell script "{script.name}" with administrator privileges',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            output = (stdout.decode() + stderr.decode()).strip()
            logger.info("%s result (rc=%d): %s", label, proc.returncode, output[:300])

            if proc.returncode != 0:
                return {"status": "error", "message": f"{label} failed: {output[:200]}"}
            return {"status": "ok", "message": f"{label} successful"}
        finally:
            os.unlink(script.name)

    async def start_tunnel(self) -> dict:
        """Start a tunnel to paired devices. Runs in background with admin privileges."""
        result = await self._run_privileged(
            f"{sys.executable} -m pymobiledevice3 remote start-tunnel "
            f"&> /tmp/catapult_tunnel.log & echo $!",
            "Tunnel",
        )
        if result.get("status") == "ok":
            await asyncio.sleep(3)  # Wait for tunnel to establish
        return result

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
