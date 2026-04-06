import asyncio
from pathlib import Path

from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.lockdown import create_using_remote
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from zeroconf import ServiceBrowser, Zeroconf, ServiceListener


MDNS_SERVICE = "_remotepairing._tcp.local."


class DeviceListener(ServiceListener):
    def __init__(self):
        self.devices: dict[str, dict] = {}

    def add_service(self, zc: Zeroconf, stype: str, name: str):
        info = zc.get_service_info(stype, name)
        if info:
            addresses = [addr for addr in info.parsed_scoped_addresses() if addr]
            if addresses:
                self.devices[name] = {
                    "name": name.replace(f".{stype}", ""),
                    "host": addresses[0],
                    "port": info.port,
                    "properties": {
                        k.decode(): v.decode() if isinstance(v, bytes) else v
                        for k, v in info.properties.items()
                    },
                }

    def remove_service(self, zc: Zeroconf, stype: str, name: str):
        self.devices.pop(name, None)

    def update_service(self, zc: Zeroconf, stype: str, name: str):
        self.add_service(zc, stype, name)


class DeviceManager:
    def __init__(self):
        self._cache: dict[str, dict] = {}

    async def discover(self, timeout: float = 3.0) -> list[dict]:
        def _scan():
            zc = Zeroconf()
            listener = DeviceListener()
            ServiceBrowser(zc, MDNS_SERVICE, listener)

            import time
            time.sleep(timeout)

            results = list(listener.devices.values())
            zc.close()
            return results

        devices = await asyncio.to_thread(_scan)

        # Also try pymobiledevice3 remote discovery
        try:
            from pymobiledevice3.remote.browse import browse_remotepairing
            rsd_devices = await asyncio.to_thread(lambda: list(browse_remotepairing(timeout=timeout)))
            for rsd in rsd_devices:
                udid = getattr(rsd, "udid", None) or str(rsd)
                if not any(d.get("udid") == udid for d in devices):
                    devices.append({
                        "name": getattr(rsd, "name", udid),
                        "udid": udid,
                        "host": getattr(rsd, "hostname", ""),
                        "port": getattr(rsd, "port", 0),
                        "connection": "network",
                    })
        except Exception:
            pass

        for d in devices:
            udid = d.get("udid") or d.get("properties", {}).get("UniqueDeviceID", d["name"])
            d["udid"] = udid
            self._cache[udid] = d

        return devices

    async def get_device_info(self, udid: str) -> dict:
        if udid in self._cache:
            return self._cache[udid]
        devices = await self.discover()
        for d in devices:
            if d.get("udid") == udid:
                return d
        raise RuntimeError(f"Device {udid} not found on the network")

    async def install(self, udid: str, ipa_path: Path):
        device = await self.get_device_info(udid)
        host = device["host"]
        port = device.get("port", 62078)

        def _install():
            rsd = RemoteServiceDiscoveryService((host, port))
            rsd.connect()
            try:
                lockdown = create_using_remote(rsd)
                installer = InstallationProxyService(lockdown=lockdown)
                installer.install_from_local(str(ipa_path))
            finally:
                rsd.close()

        await asyncio.to_thread(_install)
