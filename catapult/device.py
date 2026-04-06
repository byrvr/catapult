"""Network device discovery and app installation via pymobiledevice3."""

import asyncio
import logging
from pathlib import Path

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

logger = logging.getLogger(__name__)

# Apple devices advertise multiple mDNS services on the local network.
MDNS_SERVICES = [
    "_remotepairing._tcp.local.",      # iOS 17+ / tvOS 17+
    "_apple-mobdev2._tcp.local.",       # WiFi-synced iOS devices
    "_airplay._tcp.local.",             # Apple TV (fallback discovery)
]

DEVICE_CLASS_MAP = {
    "AppleTV": "tvos",
    "iPhone": "ios",
    "iPad": "ipados",
}


class _Listener(ServiceListener):
    def __init__(self):
        self.found: dict[str, dict] = {}

    def add_service(self, zc: Zeroconf, stype: str, name: str):
        info = zc.get_service_info(stype, name)
        if not info:
            return
        addresses = [a for a in info.parsed_scoped_addresses() if a]
        if not addresses:
            return

        props = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in info.properties.items()
        }

        device_name = props.get("model", "") or props.get("deviceName", "") or name.split(".")[0]
        udid = props.get("UniqueDeviceID") or props.get("deviceid") or props.get("pk") or name
        device_class = "unknown"
        for prefix, cls in DEVICE_CLASS_MAP.items():
            if prefix.lower() in device_name.lower() or prefix.lower() in props.get("model", "").lower():
                device_class = cls
                break

        key = f"{addresses[0]}:{info.port}"
        self.found[key] = {
            "name": device_name,
            "udid": udid,
            "host": addresses[0],
            "port": info.port,
            "service": stype,
            "device_class": device_class,
            "connection": "network",
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
            browsers = [ServiceBrowser(zc, svc, listener) for svc in MDNS_SERVICES]

            import time
            time.sleep(timeout)

            results = list(listener.found.values())
            zc.close()
            return results

        logger.info("Scanning network for devices (%ss timeout)...", timeout)
        devices = await asyncio.to_thread(_scan)

        # Supplement with pymobiledevice3's remote browse
        try:
            pmd3_devices = await self._browse_pmd3(timeout)
            seen_hosts = {d["host"] for d in devices}
            for d in pmd3_devices:
                if d["host"] not in seen_hosts:
                    devices.append(d)
        except Exception as e:
            logger.debug("pymobiledevice3 browse failed: %s", e)

        # Update cache
        for d in devices:
            self._cache[d["udid"]] = d

        logger.info("Found %d device(s)", len(devices))
        return devices

    async def _browse_pmd3(self, timeout: float) -> list[dict]:
        def _do():
            results = []
            try:
                from pymobiledevice3.remote.browse import browse_remotepairing
                for rsd in browse_remotepairing(timeout=timeout):
                    results.append({
                        "name": getattr(rsd, "name", str(rsd)),
                        "udid": getattr(rsd, "udid", str(rsd)),
                        "host": getattr(rsd, "hostname", ""),
                        "port": getattr(rsd, "port", 0),
                        "service": "remotepairing",
                        "device_class": "unknown",
                        "connection": "network",
                        "properties": {},
                    })
            except ImportError:
                logger.debug("pymobiledevice3.remote.browse not available")
            return results
        return await asyncio.to_thread(_do)

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

        logger.info("Installing to %s (%s:%s via %s)", device["name"], host, port, service)

        def _install():
            if "remotepairing" in service:
                self._install_via_rsd(host, port, ipa_path)
            else:
                self._install_via_lockdown(host, port, ipa_path)

        await asyncio.to_thread(_install)
        logger.info("Installation complete")

    def _install_via_rsd(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.lockdown import create_using_remote
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        rsd = RemoteServiceDiscoveryService((host, port))
        rsd.connect()
        try:
            lockdown = create_using_remote(rsd)
            installer = InstallationProxyService(lockdown=lockdown)
            installer.install_from_local(str(ipa_path))
        finally:
            rsd.close()

    def _install_via_lockdown(self, host: str, port: int, ipa_path: Path):
        from pymobiledevice3.lockdown import create_using_tcp
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        lockdown = create_using_tcp(host, port)
        installer = InstallationProxyService(lockdown=lockdown)
        installer.install_from_local(str(ipa_path))
