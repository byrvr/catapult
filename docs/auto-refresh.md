# Auto-Refresh (7-Day Renewal)

Free Apple Developer accounts sign apps with certificates that expire **every 7 days**. Catapult automatically re-signs and re-installs registered apps before they expire.

## How It Works

```
Server startup
      │
      ├─ restore_session()     ← load Apple ID session from ~/.catapult/state.json
      └─ run_refresh_loop()    ← background asyncio task (checks every hour)
                │
                ├─ load_state()
                ├─ find installs where now - last_installed >= 6.5 days
                └─ for each due install:
                       _refresh_install(rec)
                           ├─ get_or_create_cert
                           ├─ get_real_udid (from tunneld)
                           ├─ register_device
                           ├─ register_app_id
                           ├─ create_profile
                           ├─ signer.sign
                           ├─ device.install
                           └─ record_install (update last_installed)
```

## Timing

- **Check interval**: every 1 hour
- **Refresh threshold**: 6.5 days after last install (12h safety margin before 7-day expiry)
- **Startup delay**: 30 seconds (lets server finish initializing)

## Persistent State

All state is stored in `~/.catapult/state.json`:

```json
{
  "session": {
    "apple_id": "user@example.com",
    "adsid": "001094-10-cebded",
    "dsprsid": "...",
    "idms_token": "...",
    "gs_token": "...",
    "sk": "aabbccdd...",
    "c": "eeff0011...",
    "authenticated": true
  },
  "installs": [
    {
      "device_udid": "2F433D19-...",
      "ipa_path": "/tmp/catapult_uploads/stremio_tvOS.ipa",
      "device_name": "Living Room",
      "last_installed": 1712345678.0
    }
  ]
}
```

`sk` and `c` are stored as hex strings (they're raw bytes from the GSA auth flow).

## Session Persistence

On successful login (after team fetch), the session is written to disk:
```python
refresh.save_session(auth_client.session)
```

On server startup, it's restored:
```python
refresh.restore_session(auth_client)
```

This means the server can restart without requiring re-authentication, and the auto-refresh loop can run unattended.

**Security note**: Session tokens are stored in plaintext in `~/.catapult/state.json`. This is equivalent to how Xcode stores its credentials. The file is only readable by the user who created it (default macOS permissions).

## Install Recording

After every successful WebSocket install:
```python
refresh.record_install(device_udid, ipa_path, device_info["name"])
```

This creates or updates a record. If the same device+IPA combination is installed again, only `last_installed` is updated.

## Background Refresh Requirements

For auto-refresh to work while the server is running:

1. **Apple ID session** must be valid (not expired)
2. **tunneld** must be running (started during Setup)
3. **Device** must be on the network and reachable
4. **IPA file** must still exist at the recorded path

If any step fails, the error is logged and the loop continues. The install record is only updated on success.

## Server Mode

Run Catapult as a persistent headless server:
```bash
uv run python run.py --serve
```

Or install as a macOS LaunchAgent (auto-starts at login, restarts on crash):
```bash
uv run python run.py --install-agent
```

The LaunchAgent writes to `~/Library/LaunchAgents/com.catapult.server.plist` and logs to `~/.catapult/server.log`.
