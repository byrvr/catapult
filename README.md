# Catapult

Native macOS sideloading workspace for iOS and tvOS devices.

Catapult installs `.ipa` files onto nearby iPhone, iPad, and Apple TV devices by wrapping Apple Developer provisioning, IPA signing, device discovery, pairing, tunneling, and installation in one Mac app. It is built for free Apple Developer accounts and refreshes installed apps before the 7-day signing window expires.

## Highlights

- Native SwiftUI macOS app with a menu bar status item
- Local Python engine for Apple authentication, provisioning, signing, and device install
- iPhone/iPad install over USB or direct local discovery when available
- Apple TV pairing and tunnel setup through `pymobiledevice3`
- IPA upload, signing progress, install progress, and actionable failure messages
- Developer account view with App ID usage, expiry timestamps, delete, and reinstall controls
- Opportunistic auto-refresh starting 72 hours before app expiry
- Optional encrypted cross-device IPA vault sync
- Styled macOS DMG release package

## Download

Download the latest `Catapult-*.dmg` from [GitHub Releases](https://github.com/byrvr/catapult/releases).

The current release is ad-hoc signed for local use. If macOS Gatekeeper blocks the first launch, open it from Finder with Control-click -> Open.

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
./native/CatapultNative/Scripts/build-dmg.sh
```

Build outputs are written next to the repository:

```text
../outputs/Catapult.app
../outputs/Catapult.app.zip
../outputs/Catapult-0.3.6.dmg
```

To run the native app directly during development:

```bash
cd native/CatapultNative
swift run
```

## How It Works

The Swift app supervises a local FastAPI backend at `127.0.0.1:9450`. The backend handles Apple ID session restore, Developer Services requests, mDNS discovery, Apple TV remote pairing, tunnel creation, IPA signing, installation, and the background refresh loop.

Installed apps are recorded in `~/.catapult/state.json`. Apple auth secrets are stored in the macOS Keychain where possible; the app uses saved session data so you do not need to sign in every time.

For cross-device recovery, Catapult can store encrypted IPA blobs and an encrypted refresh manifest in a sync folder or Cloudflare R2-compatible bucket. The second Mac still needs the same Apple ID and the same `CATAPULT_SYNC_KEY` to decrypt the vault.

Finder-launched builds also read persistent sync settings from `~/.catapult/config.env`, so Catapult can keep its sync configuration after normal relaunches.

## Docs

- [Running Catapult](docs/running.md)
- [Native Swift app](docs/native-swift.md)
- [Auto-refresh behavior](docs/auto-refresh.md)
- [Cross-device refresh sync](docs/cross-device-sync.md)
- [Device pairing and tunnel notes](docs/device-pairing-tunnel.md)
- [REST and WebSocket API](docs/api.md)
- [Architecture overview](docs/architecture.md)

## Notes

Free Apple Developer accounts have a 10 App ID limit and 7-day install validity. Catapult can refresh existing installs, but Apple still controls account limits, signing validity, Developer Mode requirements, and trust prompts on devices.
