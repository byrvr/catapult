# Native Swift App

Catapult now includes a native SwiftUI macOS shell in `native/CatapultNative`.
The Swift app owns the desktop experience and starts the existing Python engine
as a local backend. This keeps the fragile Apple provisioning, signing, and
`pymobiledevice3` protocol code isolated while replacing the browser/pywebview UI.

## Run From Source

```bash
cd native/CatapultNative
swift run
```

The app finds the repository root, starts:

```bash
uv run python run.py --serve --port 9450
```

and then talks to the REST/WebSocket API on `127.0.0.1:9450`.

## Build a macOS App Bundle

```bash
./native/CatapultNative/Scripts/build-app.sh
open dist/Catapult.app
```

The bundle copies the backend into `Catapult.app/Contents/Resources/backend`.
When launched from the bundle, the backend virtual environment and uv cache are
stored under:

```text
~/Library/Application Support/Catapult/
```

so the app bundle can remain read-only after being moved to `/Applications`.

## Build a Release DMG

```bash
./native/CatapultNative/Scripts/build-dmg.sh
```

The script generates production icons, builds the Swift app, embeds the Python
backend, ad-hoc signs the bundle for local distribution, and writes:

```text
../outputs/Catapult.app
../outputs/Catapult.app.zip
../outputs/Catapult-0.3.0.dmg
```

## Current Boundary

Native Swift:

- app lifecycle and backend supervision
- device list and setup PIN workflow
- Apple ID sign-in and 2FA UI
- native IPA file picker and drag/drop upload
- WebSocket install progress
- developer account/App ID overview

Python backend:

- GSA/SRP Apple authentication
- Anisette headers
- Developer Services provisioning
- mDNS and pymobiledevice3 tunneling/install
- IPA signing with `codesign`
- auto-refresh scheduler
