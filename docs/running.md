# Running Catapult

## Requirements

- macOS 13+ (uses AOSKit for Anisette — no Docker needed)
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

### Native App (default)
Opens a native macOS window via pywebview:
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

After the first successful install, Catapult will automatically refresh the app every 7 days as long as the server is running.

## First-Time Setup (iPhone/iPad)

iPhones with the `_apple-mobdev2` mDNS service are directly installable — no pairing or tunnel needed.

1. Sign in with Apple ID
2. Upload IPA
3. Select device
4. Click Install

## CLI Flags

| Flag | Description |
|------|-------------|
| `--browser` | Open UI in browser instead of native window |
| `--serve` | Run headless (no window or browser) |
| `--install-agent` | Install macOS LaunchAgent for auto-start at login |
| `--verbose` / `-v` | Enable debug logging |
| `--port N` | Server port (default: 9450) |

## File Locations

| Path | Contents |
|------|----------|
| `~/.catapult/state.json` | Persisted Apple ID session + install records |
| `~/.catapult/server.log` | Logs when running as LaunchAgent |
| `~/.pymobiledevice3/remote_*.plist` | Apple TV pair records |
| `~/Library/LaunchAgents/com.catapult.server.plist` | LaunchAgent config |
| `/tmp/catapult_uploads/` | Uploaded IPA files |
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
