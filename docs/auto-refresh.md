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
                ├─ find installs with 72h or less before expiry
                └─ for each due install:
                       _refresh_install(rec)
                           ├─ get_or_create_cert
                           ├─ get_real_udid (from tunneld)
                           ├─ register_device
                           ├─ register_app_id
                           ├─ create_profile
                           ├─ resolve IPA from durable vault
                           ├─ signer.sign
                           ├─ device.install
                           └─ record_install (update last_installed)
```

## Timing

- **Check interval**: every 1 hour, measured against the **wall clock**
- **Refresh window**: starts 72 hours before expiry
- **Expiry**: read from the provisioning profile's `ExpirationDate` where available, falling back to install time + 7 days for records written before that was stored. The 7-day clock starts when Apple issues the profile, not when the app is installed, so the estimate runs optimistic.
- **Refresh behavior**: the hourly checker refreshes at the first successful opportunity inside that window
- **On failure**: exponential backoff, 15 minutes doubling to a 12-hour cap, recorded as `next_attempt_at`. A record is never permanently retired — previously three consecutive failures (a weekend away from the device) ended auto-refresh for good.
- **Startup delay**: 30 seconds (lets server finish initializing)

### Why the wall clock matters

The loop used to wait with `asyncio.sleep(3600)`. asyncio's clock is
`time.monotonic()`, which on macOS is `mach_absolute_time()` and **does not
advance while the system is asleep**. On one developer Mac,
`mach_continuous_time` read 3107.88 h against `mach_absolute_time`'s 2049.48 h —
44.1 days uncounted across 129 days of uptime, so the loop lost 34% of
wall-clock time and a laptop that slept nightly checked far less than hourly.

It now waits toward a wall-clock deadline in 60-second slices, so a machine
that suspends past the deadline runs on its next wake.

## Sleep and wake

Catapult holds a `PreventUserIdleSystemSleep` power assertion for the duration
of each refresh cycle, so a refresh cannot be suspended mid-signing. This needs
no entitlement and, unlike `caffeinate -s`, is honoured on battery.

It cannot wake a Mac that is already fully asleep. Scheduling a wake requires
root, so Catapult shows the command rather than running it (Settings → Sync):

```bash
sudo pmset repeat wake MTWRFSU 03:00:00
```

What that can and cannot do, from `xnu`'s `IOPMrootDomain`
(`shouldSleepOnRTCAlarmWake` returns `!acAdaptorConnected && !clamshellSleepDisableMask`,
and the clamshell path re-sleeps unconditionally on RTC wake):

| Configuration | Scheduled wake |
|---|---|
| Any desktop Mac | Works |
| MacBook on power, lid open or closed | Works |
| MacBook on battery, lid open | Works |
| MacBook on battery, lid closed | Re-sleeps immediately — not overridable |

The honest summary is "Catapult wakes your Mac to refresh, if it is plugged in
or is a desktop", not "Catapult refreshes while your Mac sleeps". Given the
72-hour window, being plugged in overnight every third night is enough.

## Untethered refresh

Catapult does **not** refresh apps while every one of your Macs is off, and
cannot. Signing shells out to `/usr/bin/codesign`, and anisette mints a fresh
~30-second `X-Apple-I-MD` from AOSKit on each request — both require a running
Mac. A cloud worker would therefore have to be a rented Mac holding your Apple
ID session and signing key.

Note that installation is *not* the obstacle: `installation_proxy` is a classic
lockdown service reachable over plain TCP, so a remote host can reach a device
that dials out. The blocker is signing.

If you want genuine 24/7 coverage, leave any Mac on the LAN running Catapult.
That is also the only option that covers **Apple TV** — SideStore and similar
on-device refreshers are iPhone/iPad only.

## Certificates

Development certificates are valid for a **year**; only the provisioning
profile carries the 7-day clock. Catapult persists the certificate and key in
the Keychain and reuses them while Apple still lists the certificate and expiry
is more than 7 days out.

When no usable certificate is stored, Catapult submits a certificate request
first and revokes existing certificates only if Apple answers that the slot is
taken (result code 7460), then retries once. On a paid team the revocation is
limited to certificates Catapult itself created. Revoking used to happen up
front on every refresh, which invalidated the certificate belonging to any
other machine or tool on the same Apple ID — Xcode, AltStore, a second Mac —
and two Catapult Macs on one Apple ID took turns doing it to each other.

Note Apple's free-tier limits, which a scheduled loop can otherwise exhaust:
10 App ID registrations per 7 days, 3 test devices per platform, 3 active apps.
App IDs are looked up before being registered for this reason.

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
      "ipa_path": "/Users/user/Library/Application Support/Catapult/IPAs/7d793....ipa",
      "ipa_sha256": "7d793...",
      "ipa_size": 123456789,
      "original_filename": "stremio_tvOS.ipa",
      "device_name": "Living Room",
      "last_installed": 1712345678.0,
      "expires_at": 1712950478.0,
      "refresh_after": 1712691278.0,
      "refresh_window_hours": 72
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

**Security note**: Session metadata is stored in `~/.catapult/state.json`; auth tokens are stored in the macOS Keychain.

## Install Recording

After every successful WebSocket install:
```python
refresh.record_install(device_udid, ipa_path, device_info["name"])
```

This creates or updates a record. The IPA is copied into the durable
content-addressed vault under `~/Library/Application Support/Catapult/IPAs/`
and matched by SHA-256 on later installs. If the same device+IPA combination is
installed again, `last_installed`, `expires_at`, and `refresh_after` are updated.

## Background Refresh Requirements

For auto-refresh to work while the server is running:

1. **Apple ID session** must be valid (not expired)
2. **tunneld** must be running (started during Setup)
3. **Device** must be on the network and reachable
4. **IPA file** must still exist in the durable local vault or be recoverable
   from configured cross-device sync

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
