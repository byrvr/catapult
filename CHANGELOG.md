# Changelog

## 0.3.4 - 2026-06-23

- Fixed duplicate main windows by using a single native window scene and removing automatic menu bar window opening on launch.

## 0.3.3 - 2026-06-22

- Fixed native launch startup so the main window and menu bar extra do not both start the engine.
- Changed packaged startup to prepare the backend environment before starting the LaunchAgent, avoiding a second foreground Python backend instance.

## 0.3.2 - 2026-06-22

- Simplified the DMG installer background to a minimal Finder-native layout without raster text or decorative panels.

## 0.3.1 - 2026-06-22

- Reworked the DMG installer background with a cleaner light layout and better label contrast.

## 0.3.0 - 2026-06-22

- Added a durable local IPA vault under Application Support so auto-refresh no longer depends on temporary upload paths.
- Added optional encrypted cross-device sync for install manifests and IPA blobs using a sync folder or Cloudflare R2.
- Added sync status to the native developer account view, including missing-key and wrong-key states.
- Added sync REST endpoints for status and manual merge/recovery.
- Updated auto-refresh, API, architecture, and running docs for vault-backed installs.

## 0.2.0 - 2026-06-18

- Added the native SwiftUI macOS app.
- Added backend supervision from the native app and improved engine status states.
- Added IPA drag/drop and native file picker upload flow.
- Added iOS, iPadOS, tvOS, AirPlay-only, direct, USB, and tunnel-aware device handling.
- Added Apple TV setup, pairing status, tunnel status, and post-pair readiness refreshes.
- Added persistent Apple ID session restore and macOS Keychain-backed auth storage.
- Added WebSocket install progress and clearer install failure messages.
- Added developer account overview with App ID expiry, delete, reload, and reinstall controls.
- Added opportunistic auto-refresh once an install has 72 hours or less before expiry.
- Added native icon assets, menu bar icon, and styled DMG packaging.

## 0.1.0 - 2026-06-17

- Initial Python/FastAPI sideloading prototype.
