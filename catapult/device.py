"""Network device discovery and app installation via pymobiledevice3."""

import asyncio
import logging
from pathlib import Path

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

logger = logging.getLogger(__name__)

# mDNS services for device discovery
MDNS_SERVICES = [
    "_remotepairing._tcp.local.",      # iOS 17+ / tvOS 17+ (paired devices)
    "_apple-mobdev2._tcp.local.",       # WiFi-synced iOS devices
    "_companion-link._tcp.local.",      # Apple TV companion
    "_airplay._tcp.local.",             # Apple TV (fallback — no install support)
]

DEVICE_CLASS_MAP = {
    "AppleTV": "tvos",
    "iPhone": "ios",
    "iPad": "ipados",
}

# Services that actually support app installation
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

        device_name = props.get("model", "") or props.get("rpMd", "") or props.get("deviceName", "") or name.split(".")[0]
        udid = props.get("UniqueDeviceID") or props.get("rpMRtID") or props.get("deviceid") or name
        device_class = "unknown"
        for prefix, cls in DEVICE_CLASS_MAP.items():
            if prefix.lower() in device_name.lower() or prefix.lower() in props.get("model", "").lower():
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

    def remove_service(self, zc: Zeroconf, stype: str, name: str):
        pass

    def update_service(self, zc: Zeroconf, stype: str, name: str):
        self.add_service(zc, stype, name)


class DeviceManager:
    def __init__(self):
        self._cache: dict[str, dict] = {}

    async def discover(self, timeout: float = 4.0) -> list[dict]:
        """Scan the local network for iOS/tvOS devices via mDNS."""
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
        raw_devices = await asyncio.to_thread(_scan)

        # Deduplicate by host — prefer installable services over AirPlay
        by_host: dict[str, dict] = {}
        for d in raw_devices:
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

    async def install(self, udid: str, ipa_path: Path):
        """Install a signed IPA to a network device."""
        device = await self.get_device_info(udid)
        host = device["host"]
        port = device.get("port", 62078)
        service = device.get("service", "")
        installable = device.get("installable", False)

        if not installable:
            raise RuntimeError(
                f"Device '{device['name']}' was found via {service} which doesn't support app installation.\n\n"
                f"For Apple TV, you need to:\n"
                f"1. Enable Developer Mode: Settings → Privacy & Security → Developer Mode\n"
                f"2. Pair with your Mac: sudo pymobiledevice3 remote pair\n"
                f"3. Start a tunnel: sudo pymobiledevice3 remote start-tunnel\n"
                f"Then restart Catapult and try again."
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
