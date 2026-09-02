# Changelog

## Unreleased

### iPhone and iPad

- Fixed selecting a Wi-Fi iPhone or iPad while an Apple TV tunnel was active installing the app **onto the Apple TV**.
- An iPhone or iPad found only over mDNS is no longer advertised as installable. Selecting one used to fire an admin password prompt, poll for roughly three minutes, and fail; it now says the device needs pairing by cable once.
- An untrusted device plugged in over USB is no longer labelled "iPhone" regardless of what it is, and the row now says to unlock it and tap Trust.
- Device scanning no longer re-triggers the Trust dialog on every poll.
- A slow or failed network scan no longer hides devices that are plugged in.
- Device registration uses the UDID reported by the device rather than the usbmux list serial.
- Running Setup for an Apple TV no longer marks every Wi-Fi iPhone and iPad on the network as ready to install.
- Installing to a Wi-Fi iPhone or iPad that advertises the Apple TV pairing service now says it needs one cable pairing, instead of prompting for an admin password and polling for a tunnel that never comes.
- Setup on an untrusted USB iPhone or iPad asks the device to trust this Mac (the button reads "Trust"), instead of failing or starting an Apple TV pairing browse.

### Auto-refresh

- Fixed the refresh loop losing all time the Mac spent asleep: it waited on a clock that stops during sleep, which on one machine went uncounted for 44 of 129 days.
- Expiry now comes from the provisioning profile's real `ExpirationDate` instead of assuming install time plus seven days.
- A failing refresh backs off exponentially instead of being retired permanently after three attempts.
- Catapult reuses its signing certificate instead of revoking every certificate on the account hourly, which used to break Xcode, AltStore, and any second Mac. Certificates are valid for a year; only the profile expires weekly.
- App IDs are looked up before being registered, so a scheduled refresh cannot exhaust Apple's limit of 10 registrations per 7 days.
- A power assertion is held across each refresh so it cannot be suspended mid-signing.
- Installing an app that is also installed from the App Store no longer tries to replace the App Store copy on the second and later installs.
- Settings → Sync shows the `pmset repeat wake` command for scheduling a nightly wake, so refreshes can run while the Mac sleeps.
- Catapult asks Apple for a new certificate before revoking anything, and revokes only when Apple reports the slot is taken. Two Macs on one Apple ID no longer take turns revoking each other's certificate.
- Keychain writes no longer place the secret on the `security` command line.

### Cross-device sync

- Sync is configured in Settings instead of environment variables and `~/.catapult/config.env`, which a Finder-launched app never inherited.
- The vault defaults to your own iCloud Drive, and a second Mac is unlocked with a single recovery key rather than a hand-copied shared secret.
- Fixed a second Mac silently creating an incompatible vault when it had no key, instead of reporting the existing vault as locked.
- Blobs are encrypted in streamed chunks rather than whole in memory, which needed roughly 3 GB for a 1 GB IPA.
- Writes are staged outside the synced folder, so a partial upload is no longer pushed to your other Macs.
- Existing `CATAPULT_SYNC_KEY` vaults are adopted automatically with no re-upload.
- Removed the DMG option that embedded R2 credentials in the disk image.
- "Start a new vault" asks for confirmation and keeps the old vault beside the new one, instead of silently overwriting the vault descriptor and leaving every Mac reporting a wrong key.
- A first Mac now sees "Create vault" instead of "This vault is locked" when the sync folder has no vault yet.
- A Mac that still uses the old `CATAPULT_SYNC_KEY` can open a vault another Mac migrated from that key, and Settings → Sync can show the recovery key this Mac holds.
- A failed sync no longer blanks the Developer Account view.
- Two Macs sharing a vault take turns: the hourly refresh holds a short lease in the vault and skips a cycle another Mac is running.
- S3-compatible buckets accept a `region`, and IPA blobs stream to and from them instead of being held in memory.

### Store

- New Store tab: add a GitHub repository (or its releases page) or an AltStore source, see the builds that fit the selected device, and install or update them through the normal signing pipeline. Repositories that publish several apps under fixed tags are supported.
- Store installs show progress and errors on the Store tab.
- Installed store apps can opt into daily automatic updates from the Store tab; an update installs only when the device is reachable, never asks for an admin password, and yields to another Mac's refresh lease.
- Downloads are verified against the release's `SHA256SUMS` file when it publishes one, and against the digest an AltStore source publishes.
- Apps you installed before, on any device and even by hand before the Store existed, are marked with the devices they went to, and the catalog can be filtered to All, Installed, or New. Install records now carry the app version so those matches keep working.

### Apple TV tunnel

- Catapult no longer asks for an admin password on every tunnel. A wedged tunneld is recovered through its local control endpoints, and the hourly refresh never installs the daemon.
- A background refresh that finds tunneld missing no longer blocks the next Setup click for a minute.
- tunneld is restarted only when it serves no tunnel at all; an unreachable Apple TV no longer tears down another Apple TV's live tunnel.
- With two Apple TVs, the cached tunnel is used only for the Apple TV it was opened for, and Catapult no longer guesses which of them a lone unidentified tunnel belongs to.

### Signing

- Nested app extensions and frameworks are signed under their own bundle identifiers.

## 0.3.8 - 2026-07-17

- Automatically restart a wedged tunnel helper instead of trusting it: a long-running tunneld could silently stop discovering devices while still answering on its port, making Apple TV "Connect" time out with a misleading "device may need re-pairing".
- Widen the tunnel poll window after a helper (re)start so a cold tunneld has time to rediscover and re-tunnel the device before giving up.

## 0.3.7 - 2026-06-25

- Show expired and history-only installs in the Developer Account view even when Apple no longer lists the App ID.
- Keep recovered local/synced installs reinstallable when their saved IPA is still available.
- Label expired/history rows clearly in the native account sheet.
- Keep public DMGs free of user-specific R2 credentials while supporting private encrypted sync handoff packages.

## 0.3.6 - 2026-06-24

- Added persistent `~/.catapult/config.env` support for sync settings in Finder-launched packaged apps.

## 0.3.5 - 2026-06-23

- Fixed iPhone transport labels so Wi-Fi-paired usbmux devices show as Wi-Fi instead of USB.
- Stopped showing internal `usb:<udid>` endpoints in the native device row.

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
