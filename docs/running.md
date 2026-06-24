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
swift run
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
IPA is available. Configure encrypted sync to store a remote IPA vault.

### Sync Folder

```bash
export CATAPULT_SYNC_PROVIDER=folder
export CATAPULT_SYNC_KEY="choose-a-long-shared-recovery-key"
export CATAPULT_SYNC_FOLDER="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Catapult"
```

For Finder-launched packaged apps, put the same values in
`~/.catapult/config.env` so the background engine can read them after relaunch:

```text
CATAPULT_SYNC_PROVIDER=folder
CATAPULT_SYNC_KEY=choose-a-long-shared-recovery-key
CATAPULT_SYNC_FOLDER=/Users/you/Library/Mobile Documents/com~apple~CloudDocs/Catapult
```

### Cloudflare R2

```bash
export CATAPULT_SYNC_PROVIDER=r2
export CATAPULT_SYNC_KEY="choose-a-long-shared-recovery-key"
export CATAPULT_R2_ENDPOINT="https://<account-id>.r2.cloudflarestorage.com"
export CATAPULT_R2_BUCKET="catapult"
export CATAPULT_R2_ACCESS_KEY_ID="..."
export CATAPULT_R2_SECRET_ACCESS_KEY="..."
```

Use the same `CATAPULT_SYNC_KEY` on every Mac that should decrypt and refresh
the same IPA vault.

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
