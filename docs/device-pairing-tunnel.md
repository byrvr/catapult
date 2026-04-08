# Device Discovery, Pairing & Tunnel

## Device Discovery (mDNS)

Catapult scans the local network using mDNS (Bonjour) via the `zeroconf` library. It listens for four service types:

| Service | Device Type | Installable? |
|---------|-------------|-------------|
| `_apple-mobdev2._tcp.local.` | iPhone/iPad | Yes — direct TCP lockdown |
| `_remotepairing._tcp.local.` | Apple TV (tvOS) | After pairing + tunnel |
| `_companion-link._tcp.local.` | Any Apple device | No — info only |
| `_airplay._tcp.local.` | Any Apple device | No — info only |

Devices discovered from multiple services are deduplicated by host IP. The deduplication priority is `installable > needs_setup > other`.

Device names are extracted from the mDNS service name itself (e.g., `"Living Room._companion-link._tcp.local."` → `"Living Room"`). Model and class are detected from the `model` and `rpMd` TXT record properties.

## Apple TV Setup Flow

Apple TV uses the **Remote Pairing** protocol. Installation requires:
1. Pair with the device (exchange keys, get PIN)
2. Start a tunnel (RSD over TLS-PSK through a TUN interface)
3. Install via the tunnel

### Step 1: Pairing

```
Client                                          Apple TV
  │                                                │
  │── browse _remotepairing-manual-pairing ──────► │
  │◄─ device responds with IP, port, identifier ── │
  │                                                │
  │── RemotePairingManualPairingService.connect() ►│
  │                                                │── shows PIN on screen
  │◄────────────── waiting for PIN ───────────────  │
  │                                                │
  │   [user enters PIN in Catapult web UI]         │
  │                                                │
  │── PIN submitted ──────────────────────────────►│
  │◄── RemotePairingCompletedError (= success) ────│
  │                                                │
  │  Pair record saved to:                         │
  │  ~/.pymobiledevice3/remote_{identifier}.plist  │
```

**PIN input implementation**: The `pymobiledevice3` pairing API calls `input()` to block waiting for the PIN. Catapult monkey-patches `builtins.input` with a function that blocks on a `threading.Event`. When the user submits the PIN via the `/api/devices/pin` endpoint, the event is set and the pairing thread unblocks.

**`RemotePairingCompletedError`**: Despite the name, this exception signals **successful** completion. When pairing finishes, the Apple TV closes the connection. `pymobiledevice3` raises this to signal the caller to reconnect.

Pair records are stored at `~/.pymobiledevice3/remote_{UUID}.plist` and contain Ed25519 key material for future pair-verify operations.

### Step 2: Tunnel (tunneld)

After pairing, a **RSD tunnel** is needed for all service connections. Catapult uses `pymobiledevice3 remote tunneld` as a persistent background daemon.

**Why tunneld instead of `start-tunnel`?**

`start-tunnel` is a one-shot process that blocks. `tunneld` is a persistent service that:
- Auto-discovers all paired WiFi devices
- Maintains tunnels continuously
- Exposes device tunnel info via HTTP API at `http://127.0.0.1:49151`

**Why does it need admin/sudo?**

The tunnel creates a TUN virtual network interface (`utun*`) on macOS. This requires root privileges. Catapult uses `osascript -e 'do shell script "..." with administrator privileges'` to prompt for admin password once.

The tunneld command used:
```bash
HOME=/Users/{user} PYTHONUNBUFFERED=1 \
python -m pymobiledevice3 remote tunneld \
  --no-usb --no-usbmux --no-mobdev2 \
  > /tmp/catapult_tunneld.log 2>&1 &
```

`HOME` is explicitly set because osascript runs as root (`/var/root`), which would cause `pymobiledevice3` to look for pair records in `/var/root/.pymobiledevice3/` instead of the user's home directory.

### Step 3: Tunnel Query

Once tunneld is running, Catapult polls its HTTP API:

```
GET http://127.0.0.1:49151
→ {
    "02732143-4727-4959-8A12-7E2B69050E56": [
      {
        "tunnel-address": "fd45:ec19:1617::1",
        "tunnel-port": 62255,
        "interface": "fd05:74aa:65f4:4431:9:fd46:f570:e018"
      }
    ]
  }
```

The key is the **pairing UUID** (not the hardware UDID). The `tunnel-address` is an IPv6 address on the TUN interface that routes to the Apple TV's RSD service.

## Real Device UDID

The mDNS service name (`2F433D19-..._remotepairing._tcp.local.`) and the tunneld key (`02732143-...`) are **not** the device's hardware UDID. Apple's Developer Portal rejects both formats.

The real UDID is obtained by connecting to the device via RSD and reading `peer_info.Properties.UniqueDeviceID`:

```python
rsds = await get_tunneld_devices()    # connects via tunneld tunnel
rsd = rsds[0]
real_udid = rsd.udid                  # "00008110-000C65D91EF1801E"
product_type = rsd.peer_info["Properties"]["ProductType"]  # "AppleTV14,1"
```

This is also where tvOS is detected — any `ProductType` containing `AppleTV` triggers `subPlatform="tvOS"` in the provisioning profile request.

## Installation

### Via RSD (Apple TV, remotepairing)

```python
rsds = await get_tunneld_devices()
rsd = rsds[0]
installer = InstallationProxyService(lockdown=rsd)
await installer.install_from_local(ipa_path)
```

`InstallationProxyService` accepts a `LockdownServiceProvider` (which `RemoteServiceDiscoveryService` implements) directly — no intermediate lockdown client is needed for RSD connections.

### Via TCP Lockdown (iPhone/iPad, mobdev2)

```python
lockdown = create_using_tcp(host, port)
installer = InstallationProxyService(lockdown=lockdown)
await installer.install_from_local(ipa_path)
```

## Installation Progress

`install_from_local` reports progress events from `installd` on the device:
```
5% → 15% → 20% → 30% → 40% → 50% → 60% → 70% → 80% → 90% → 100%
"Installation succeed."
```

These are forwarded to the WebSocket client in real-time.

## Multiple Tunnels

`tunneld` manages tunnels for all paired WiFi devices simultaneously. When `get_tunneld_devices()` returns multiple RSDs, Catapult always uses `rsds[0]`. Since `get_real_udid()` and `_install_via_rsd()` both call `get_tunneld_devices()`, they use the same device.

The ordering from tunneld is consistent — it corresponds to the order devices were connected.
