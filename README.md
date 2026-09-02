# Catapult

Native macOS sideloading workspace for iOS and tvOS devices.

[![macOS 14+](https://img.shields.io/badge/macOS-14%2B-0A84FF)](https://www.apple.com/macos/)
[![SwiftUI](https://img.shields.io/badge/UI-SwiftUI-F05138)](native/CatapultNative)
[![Python](https://img.shields.io/badge/backend-Python-3776AB)](catapult)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

Catapult installs `.ipa` files onto nearby iPhone, iPad, and Apple TV devices by wrapping Apple Developer provisioning, IPA signing, device discovery, pairing, tunneling, and installation in one Mac app. It is built for free Apple Developer accounts and refreshes installed apps before the 7-day signing window expires.

## Highlights

- Native SwiftUI macOS app with a menu bar status item
- Local Python engine for Apple authentication, provisioning, signing, and device install
- iPhone/iPad install over USB or direct local discovery when available
- Apple TV pairing and tunnel setup through `pymobiledevice3`
- IPA upload, signing progress, install progress, and actionable failure messages
- Developer account view with App ID usage, expiry timestamps, delete, and reinstall controls
- Expired/history installs remain visible and reinstallable when Catapult still has the saved IPA
- Opportunistic auto-refresh starting 72 hours before app expiry
- Optional encrypted cross-device IPA vault, in your own iCloud Drive or any synced folder
- Styled macOS DMG release package

## Download

Download the latest public `Catapult-*.dmg` from [GitHub Releases](https://github.com/byrvr/catapult/releases).

The current release is ad-hoc signed for local use. If macOS Gatekeeper blocks the first launch, open it from Finder with Control-click -> Open.

Public releases do not include Apple ID credentials, R2 keys, sync keys, IPA files, signing identities, or device-specific configuration.

## Requirements

- macOS 14 or newer
- `uv` available on the Mac when building from source
- Free or paid Apple Developer account
- iPhone, iPad, or Apple TV on the local network
- Developer Mode enabled on Apple TV before pairing

## Build From Source

```bash
git clone https://github.com/byrvr/catapult.git
cd catapult
uv sync
./native/CatapultNative/Scripts/build-dmg.sh
```

Build outputs are written next to the repository:

```text
../outputs/Catapult.app
../outputs/Catapult.app.zip
../outputs/Catapult-0.3.8.dmg
```

To run the native app directly during development:

```bash
cd native/CatapultNative
swift run CatapultNative
```

## How It Works

The Swift app supervises a local FastAPI backend at `127.0.0.1:9450`. The backend handles Apple ID session restore, Developer Services requests, mDNS discovery, Apple TV remote pairing, tunnel creation, IPA signing, installation, and the background refresh loop.

Installed apps are recorded in `~/.catapult/state.json`. Apple auth secrets are stored in the macOS Keychain where possible; the app uses saved session data so you do not need to sign in every time.

For cross-device recovery, Catapult stores encrypted IPA blobs and an encrypted manifest in storage you already own — your iCloud Drive by default, or any folder your cloud client syncs, or an S3-compatible bucket (configured through the local API for now). A second Mac needs the same Apple ID and one recovery key, which Catapult shows when the vault is created and again on request from Settings → Sync.

Sync is configured in Settings → Sync and stored in `~/Library/Application Support/Catapult/sync.json`, so it survives relaunches without any shell environment.

## Safety Model

Catapult is local-first:

- Apple ID session material is kept on the Mac and stored in Keychain where possible.
- Signing certificates, provisioning profiles, and plaintext IPAs are not uploaded by default.
- Optional sync encrypts IPA blobs and manifests before writing to a sync folder or Cloudflare R2.
- Public release artifacts must never include user-specific sync credentials.

## Docs

- [Running Catapult](docs/running.md)
- [Native Swift app](docs/native-swift.md)
- [Auto-refresh behavior](docs/auto-refresh.md)
- [Cross-device refresh sync](docs/cross-device-sync.md)
- [Device pairing and tunnel notes](docs/device-pairing-tunnel.md)
- [REST and WebSocket API](docs/api.md)
- [Architecture overview](docs/architecture.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Notes

Free Apple Developer accounts have a 10 App ID limit and 7-day install validity. Catapult can refresh existing installs, but Apple still controls account limits, signing validity, Developer Mode requirements, and trust prompts on devices.
