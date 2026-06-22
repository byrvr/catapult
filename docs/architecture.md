# Catapult — Architecture Overview

Catapult is a macOS-native sideloading tool that installs `.ipa` files onto iOS and tvOS devices over the local network. It reproduces the full Xcode/AltStore provisioning pipeline without USB or a paid developer account.

## High-Level Components

```
┌─────────────────────────────────────────────────────────────────┐
│                         macOS Host                               │
│                                                                  │
│  ┌───────────────┐  WebSocket/REST ┌─────────────────────────┐   │
│  │ Native SwiftUI│◄───────────────►│   FastAPI Server        │   │
│  │ macOS App     │                 │   (catapult/server.py)  │   │
│  │ + menu bar    │                 └────────┬────────────────┘   │
│  └───────────────┘                          │                    │
│                              ┌────────────┼─────────────┐       │
│                              │            │             │        │
│                      ┌───────▼──┐  ┌─────▼────┐  ┌────▼─────┐ │
│                      │ AppleAuth│  │Developer │  │  Device  │ │
│                      │  (GSA/   │  │ Services │  │ Manager  │ │
│                      │  SRP-6a) │  │  (certs/ │  │ (mDNS +  │ │
│                      └───────┬──┘  │ profiles)│  │ tunnel)  │ │
│                              │     └─────┬────┘  └────┬─────┘ │
│                              │           │             │        │
│                      ┌───────▼───────────▼─────────────▼─────┐ │
│                      │           Anisette Layer               │ │
│                      │  (AOSKit native  OR  omnisette-server) │ │
│                      └───────────────────────────────────────┘ │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Background Refresh (catapult/refresh.py)                │   │
│  │  Checks ~/.catapult/state.json every hour               │   │
│  │  Re-signs and re-installs apps inside 72h expiry window │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  IPA Vault + Sync (catapult/vault.py, catapult/sync.py)  │   │
│  │  Stores durable local IPAs by SHA-256 and optionally     │   │
│  │  uploads encrypted blobs/manifests to folder or R2 sync  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
         │                                        │
         │ GSA / Developer Services               │ pymobiledevice3
         ▼                                        ▼
   Apple Servers                         Apple TV / iPhone
   gsa.apple.com                    (tunneld RSD → installd)
   developerservices2.apple.com
```

## Request Flow: Install an App

```
User clicks Install
      │
      ▼
WebSocket /ws/install
      │
      ├─ 1. Fetch team (Developer Portal)
      ├─ 2. Revoke old certs → create new CSR → submit to Apple → get cert
      ├─ 3. Get real device UDID from RSD peer info
      ├─ 4. Register device with Apple Developer Portal
      ├─ 5. Register app ID (sideload namespace: com.catapult.{team}.{bundle})
      ├─ 6. Delete stale profiles → downloadTeamProvisioningProfile
      ├─ 7. Copy IPA into durable content-addressed vault
      ├─ 8. Sign IPA (temp keychain, codesign, patch bundle ID)
      ├─ 9. Install via tunnel RSD → installd reports 100%
      ├─ 10. Record install timestamp + IPA SHA in ~/.catapult/state.json
      └─ 11. If configured, sync encrypted manifest/blob to folder or R2
```

## Module Map

| Module | Responsibility |
|---|---|
| `main.py` | CLI entry, native window, LaunchAgent |
| `server.py` | FastAPI routes, WebSocket install flow, startup hooks |
| `apple_auth.py` | Apple ID authentication via GSA/SRP-6a |
| `anisette.py` | Anisette OTP headers (native macOS AOSKit or omnisette-server) |
| `developer.py` | Developer Portal API (certs, devices, app IDs, profiles) |
| `device.py` | mDNS discovery, pairing, tunneld, installation |
| `signer.py` | IPA signing (keychain, codesign, bundle ID rewrite) |
| `ipa.py` | IPA zip handling (extract, inspect, repack) |
| `refresh.py` | Persistent state, 72h-window opportunistic auto-refresh loop |
| `vault.py` | Durable local content-addressed IPA storage |
| `sync.py` | Encrypted cross-device manifest and IPA blob sync |

## Key Data Structures

### Device Record (from mDNS scan)
```python
{
    "name": "Living Room",
    "model": "AppleTV14,1",
    "udid": "ECD3D531-..._remotepairing._tcp.local.",  # mDNS ID (not real UDID)
    "host": "192.168.100.92",
    "port": 49152,
    "service": "_remotepairing._tcp.local.",
    "device_class": "tvos",
    "connection": "network",
    "installable": False,       # True after tunnel is up
    "needs_setup": True,        # Requires pairing + tunnel
    "properties": {...},
}
```

### AuthSession
```python
@dataclass
class AuthSession:
    apple_id: str       # ruslan@gmail.com
    adsid: str          # 001094-10-cebded  (account DSid)
    dsprsid: str        # Data & Privacy record ID
    idms_token: str     # 280-char GsIdmsToken (for 2FA only)
    gs_token: str       # 196-char app-specific token (for dev services)
    sk: bytes           # 32-byte session key (from spd blob)
    c: bytes            # 168-byte cookie (for apptokens request)
    authenticated: bool
```

### Persistent State (~/.catapult/state.json)
```json
{
  "session": {
    "apple_id": "user@example.com",
    "adsid": "...",
    "gs_token": "...",
    "sk": "hex",
    "c": "hex",
    "authenticated": true
  },
  "installs": [
    {
      "device_udid": "ECD3D531-...",
      "ipa_path": "/Users/user/Library/Application Support/Catapult/IPAs/7d793....ipa",
      "ipa_sha256": "7d793...",
      "ipa_size": 123456789,
      "original_filename": "app.ipa",
      "device_name": "Living Room",
      "last_installed": 1712345678.0
    }
  ]
}
```
