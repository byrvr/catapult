# Running Catapult

## Requirements

- macOS 14+ for the native app
- Python 3.14+
- `uv` package manager
- Apple ID (free developer account)
- Apple TV or iPhone on the same local network

## Installation

```bash
git clone <repo>
cd catapult
uv sync
```

## Modes

### Native App
Build and run the SwiftUI macOS app:
```bash
cd native/CatapultNative
swift run CatapultNative
```

The native app starts the local Python engine automatically.

### Legacy Web App
Opens the browser/pywebview interface:
```bash
uv run python run.py
```

### Browser
Opens in your default browser:
```bash
uv run python run.py --browser
```

### Background Server (headless)
No window, no browser — just the server:
```bash
uv run python run.py --serve
```

Access the UI at `http://127.0.0.1:9450`.

### Auto-Start at Login
Install as a LaunchAgent:
```bash
uv run python run.py --install-agent
```

The server starts automatically at login and restarts if it crashes.
Logs go to `~/.catapult/server.log`.

## Cross-Device Sync

Catapult can recover refreshable installs on another Mac only if the original
IPA is available, so it keeps an encrypted vault in storage you already own.

Set it up in **Settings → Sync** — there is nothing to export and no file to
edit. Pick iCloud Drive (the default) or any folder your cloud client already
syncs, create a vault, and save the recovery key it shows you once.

On a second Mac, sign into the same Apple ID, open Settings → Sync, and paste
that recovery key. Universal Clipboard has usually already carried it across.

Earlier versions were configured with `CATAPULT_SYNC_*` environment variables
or `~/.catapult/config.env`. Those are still read for one release and imported
automatically on first run, then you can delete the dotfile. See
[Cross-device sync](cross-device-sync.md) for the vault format, the state
table, and the migration details.

## First-Time Setup (Apple TV)

1. **Enable Developer Mode** on the Apple TV:
   Settings → System → Developer Mode → Enable

2. **Start Catapult** and open the UI.

3. **Click "Setup"** next to the Apple TV in the device list.
   - Enter your Mac's admin password when prompted (needed to create the tunnel interface)
   - A PIN appears on the Apple TV screen — enter it in the dialog

4. **Sign in** with your Apple ID.

5. **Drop an IPA** onto the upload zone.

6. **Click Install**.

After the first successful install, Catapult checks hourly and automatically refreshes the app at the first successful opportunity once it has 72 hours or less before expiry.

## First-Time Setup (iPhone/iPad)

iPhones with the `_apple-mobdev2` mDNS service are directly installable — no pairing or tunnel needed.

1. Sign in with Apple ID
2. Upload IPA
3. Select device
4. Click Install

## CLI Flags

| Flag | Description |
|------|-------------|
| `--browser` | Open the legacy web UI in a browser |
| `--serve` | Run headless (no window or browser) |
| `--install-agent` | Install macOS LaunchAgent for auto-start at login |
| `--verbose` / `-v` | Enable debug logging |
| `--port N` | Server port (default: 9450) |

## File Locations

| Path | Contents |
|------|----------|
| `~/.catapult/state.json` | Persisted Apple ID session + install records |
| `~/.catapult/server.log` | Logs when running as LaunchAgent |
| `~/Library/Application Support/Catapult/IPAs/` | Durable content-addressed IPA vault |
| `~/.pymobiledevice3/remote_*.plist` | Apple TV pair records |
| `~/Library/LaunchAgents/com.catapult.server.plist` | LaunchAgent config |
| `~/.catapult/uploads/` | Temporary upload staging before vault import |
| `/tmp/catapult_sign_*/` | Temporary signing workdirs (cleaned up after signing) |
| `/tmp/catapult_tunneld.log` | tunneld daemon log |

## Troubleshooting

**"Setup" button doesn't appear for Apple TV**
→ Developer Mode may not be enabled on the Apple TV. Go to Settings → System → Developer Mode.

**Pairing succeeds but tunnel fails**
→ tunneld requires admin. Make sure you enter your Mac password when prompted.

**App installs but doesn't appear on Apple TV home screen**
→ Restart the Apple TV. Newly installed development apps sometimes require a reboot to appear.

**"A valid provisioning profile was not found"**
→ The device UDID wasn't registered correctly. Click Setup again to re-pair and get a fresh profile.

**Authentication fails after server restart**
→ The session in `~/.catapult/state.json` may have expired. Sign in again via the UI.
