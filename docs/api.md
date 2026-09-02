# REST & WebSocket API

The server runs at `http://127.0.0.1:9450` by default. All REST endpoints accept/return JSON unless noted.

## Device Endpoints

### `GET /api/devices`
Scan the local network for Apple devices (4-second mDNS timeout).

**Response:**
```json
{
  "devices": [
    {
      "name": "Living Room",
      "model": "AppleTV14,1",
      "udid": "2F433D19-..._remotepairing._tcp.local.",
      "host": "192.168.100.92",
      "port": 49152,
      "service": "_remotepairing._tcp.local.",
      "device_class": "tvos",
      "connection": "network",
      "installable": false,
      "needs_setup": true,
      "properties": {}
    }
  ]
}
```

`device_class` is one of `ios`, `ipados`, `tvos`, `unknown`.

---

### `POST /api/devices/setup`
For an Apple TV: pair (on-screen PIN) if needed, then start the tunnel. For an untrusted USB iPhone or iPad: ask the device to trust this Mac (the Trust prompt appears on the device) and stop there — there is no tunnel to start.

Pair with a device and start a tunnel. Required before installing on Apple TV.

**Request:**
```json
{"name": "Living Room"}
```

**Response:**
```json
{"status": "ok", "message": "Tunnel active at fd45:ec19:1617::1:62255"}
```

The pairing step shows a PIN on the Apple TV screen. Poll `/api/devices/pair-status` and submit the PIN via `/api/devices/pin`.

---

### `GET /api/devices/pair-status`
Check current pairing state.

**Response:**
```json
{"state": "waiting_pin"}
```

`state` is one of: `idle`, `browsing`, `pairing`, `waiting_pin`, `done`, `error`.

---

### `POST /api/devices/pin`
Submit the PIN shown on the device during pairing.

**Request:**
```json
{"pin": "123456"}
```

---

### `POST /api/devices/pair` / `POST /api/devices/tunnel`
Individual pair and tunnel operations (called together by `setup`).

---

## Auth Endpoints

### `GET /api/auth/status`
Check if there's an active session.

**Response:**
```json
{"authenticated": true, "apple_id": "user@example.com"}
```

---

### `POST /api/auth/login`
Sign in with Apple ID.

**Request:**
```json
{"apple_id": "user@example.com", "password": "secret"}
```

**Response (success):**
```json
{"status": "ok"}
```

**Response (2FA required):**
```json
{"status": "2fa_required", "auth_type": "trustedDeviceSecondaryAuth"}
```

---

### `POST /api/auth/2fa`
Submit 2FA verification code.

**Request:**
```json
{"code": "123456"}
```

---

## Sync Endpoints

Cross-device sync keeps an encrypted manifest and encrypted IPA blobs in
storage the user already owns: iCloud Drive or any synced folder by default,
an S3-compatible bucket as an advanced option. A vault is opened with a
recovery key. See [cross-device-sync.md](cross-device-sync.md) for the model.

### `GET /api/sync/status`
Configuration snapshot. Performs no network I/O; for a folder vault it reads
the vault descriptor on disk so a first Mac sees `needs_setup`.

`vault_state` is one of `disabled`, `needs_setup`, `needs_icloud`, `locked`,
`ok`, `wrong_key`.

**Response:**
```json
{
  "provider": "folder",
  "configured": true,
  "vault_state": "ok",
  "vault_bytes": 412000000,
  "icloud_available": true,
  "icloud_path": "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/Catapult",
  "have_recovery_key": true,
  "folder": "/Users/user/Library/Mobile Documents/com~apple~CloudDocs/Catapult",
  "r2_endpoint": "",
  "r2_bucket": "",
  "apple_id": "user@example.com",
  "team_id": "ABCDE12345"
}
```

---

### `POST /api/sync/configure`
Choose where the vault lives. Replaces the old environment variables.

**Request:** `{"provider": "folder" | "r2" | "disabled", "folder": "<path>"}`.
With `provider: "folder"` and no `folder`, the iCloud Drive path is used; that
fails with `400 needs_icloud` when iCloud Drive is off. For `r2`, also pass
`r2_endpoint`, `r2_bucket`, `r2_access_key_id`, `r2_secret_access_key` and
optionally `region` (default `auto`, which is correct for Cloudflare R2 only).
Credentials go to the Keychain, not the settings file.

**Response:** the status payload above.

---

### `POST /api/sync/create-vault`
Create the vault and return the recovery key **once**.

Answers `409` with `{"status": "exists"}` when a vault already exists, unless
the body carries `{"replace": true}`. Replacing moves the old vault aside to
`teams/<team_id>.replaced-<timestamp>` and mints a new key; every other Mac is
locked out until it gets the new key.

**Response:**
```json
{"status": "ok", "recovery_key": "CAT1-K7X2M-9QZ4B-T3VHN-8RJ5W-DGP6Y-F", "message": "Save this recovery key. Catapult cannot recover it for you."}
```

---

### `POST /api/sync/unlock`
**Request:** `{"recovery_key": "CAT1-…"}`. `200 ok` when the key opens the
vault; `400` with `wrong_key` or `needs_setup` otherwise.

---

### `GET /api/sync/recovery-key`
The recovery key this Mac already holds, for a user who lost their copy or
whose vault was migrated from `CATAPULT_SYNC_KEY` in the background. `404`
when this Mac holds none. The key only ever travels to the local UI.

---

### `POST /api/sync/run`
Merge local install state with the encrypted remote manifest, upload missing
IPA blobs, and download missing IPA blobs. Downloads are decrypted and
hash-verified before they reach the local vault.

Informational vault states (`needs_setup`, `locked`, `wrong_key`,
`needs_icloud`) come back with HTTP `200` and the state in `status`; `500` is
reserved for `error`.

**Response:**
```json
{
  "status": "ok",
  "vault_state": "ok",
  "provider": "folder",
  "configured": true,
  "uploaded_ipas": 1,
  "downloaded_ipas": 0,
  "install_count": 1,
  "vault_bytes": 412000000
}
```

**Refresh lease.** While sync is configured, the hourly refresh cycle takes
`teams/<team_id>/lease.json` (`{"locked_by", "locked_until", "operation"}`)
for at most 20 minutes and skips the cycle if another Mac holds a live lease.

---

## Store Endpoints

Sources are GitHub repositories (releases with `.ipa` assets) or AltStore
`apps.json` URLs, stored in `~/Library/Application Support/Catapult/sources.json`.

### `GET /api/store/sources`
`{"sources": [{"id", "kind", "url", "include_prerelease", "last_checked_at"}]}`.

### `POST /api/store/sources`
**Request:** `{"url": "owner/repo" | "https://github.com/owner/repo[/releases]" | "https://…/apps.json"}`.
The source is fetched once before it is saved; `400` if it cannot be read,
`409` if it is already added.

### `POST /api/store/sources/remove`
**Request:** `{"id": "github:owner/repo"}`.

### `POST /api/store/sources/update`
**Request:** `{"id": "github:owner/repo", "include_prerelease": false}`.

### `GET /api/store/apps?device_udid=…`
The merged catalog, filtered to builds that fit the selected device. Each app
carries `installed_version`, `update_available`, `auto_update` and `pinned`
for **that device**, plus `sha256` when the release publishes a `SHA256SUMS`
asset (or an AltStore source publishes a digest). `installed_before` and
`installed_on` (device names, one per device) say whether any install record
on any device matches the entry. Store installs match by their link, published
digests match exactly, and hand installs match by the app name in the file
they were installed from, by bundle id, or by the app version baked into the
tag together with the asset size (within 1%; when several tweaks of one
version qualify, the record counts for the closest one only). A hand install
whose file carried no recognisable name is matched by version, so it stops
being marked once the source publishes a newer version; installing it once
from the Store links it permanently. Loading the catalog also backfills
`app_version` on older install records from the vaulted IPA, once. Each app
also carries `icon`: the source's own icon URL, else `/api/store/icon?sha=…`
when an IPA of this entry is already on this Mac (a matched install record's
file, or the download cache) and an icon could be pulled out of it — a loose
`AppIcon*.png`, or the primary icon from `Assets.car` through the
`catapult-icon` helper (found via `CATAPULT_ICON_HELPER`, the dev build, or
the app bundle) — else the GitHub owner's avatar, else `""`. Also returns
`errors` (per source), `device_class`, and `free_team`.

### `GET /api/store/icon?sha=<64 hex>`
The icon extracted from a local IPA with that SHA-256, as `image/png` with
`Cache-Control: max-age=86400`; `404` for an unknown or malformed digest.

### `POST /api/store/apps/auto-update`
**Request:** `{"device_udid": "…", "app_key": "…", "enabled": true}`. Opts an
installed store app in or out of the daily update check; `404` when the app is
not installed from the Store on that device. Updates install only when the
device is reachable at check time.

### `WS /ws/store-install`
**Client → Server:** `{"app_key": "…", "device_udid": "…"}`. Downloads the
build, verifies it against the published digest when there is one, and hands
it to the same pipeline as `WS /ws/install`; the progress stream is identical.

---

## Power Endpoints

### `GET /api/power/wake-command?hour=4&minute=15`
Returns the `sudo pmset repeat wake …` command for a nightly wake so refreshes
can run while the Mac sleeps. Returned, never executed.

---

## File Endpoints

### `POST /api/upload`
Upload an IPA file (multipart form data, field name `file`).

**Response:**
```json
{
  "path": "/Users/user/Library/Application Support/Catapult/IPAs/7d793....ipa",
  "info": {
    "bundle_id": "com.example.app",
    "bundle_name": "My App",
    "version": "2.1.0",
    "build": "42",
    "min_os": "16.0",
    "executable": "MyApp",
    "vault": {
      "sha256": "7d793...",
      "path": "/Users/user/Library/Application Support/Catapult/IPAs/7d793....ipa",
      "size": 123456789
    }
  }
}
```

---

## WebSocket: Install

### `WS /ws/install`
Full install pipeline. Opens a WebSocket, sends one JSON message to start, receives progress updates.

**Client → Server (initial message):**
```json
{
  "device_udid": "2F433D19-...",
  "ipa_path": "/Users/user/Library/Application Support/Catapult/IPAs/7d793....ipa"
}
```

**Server → Client (progress updates):**
```json
{"step": "signing",    "progress": 0,   "message": "Fetching team..."}
{"step": "signing",    "progress": 10,  "message": "Preparing signing certificate..."}
{"step": "signing",    "progress": 25,  "message": "Registering device..."}
{"step": "signing",    "progress": 40,  "message": "Registering app ID..."}
{"step": "signing",    "progress": 50,  "message": "Creating provisioning profile..."}
{"step": "signing",    "progress": 60,  "message": "Signing IPA..."}
{"step": "installing", "progress": 80,  "message": "Installing to Living Room..."}
{"step": "done",       "progress": 100, "message": "Installed successfully!"}
```

**On error:**
```json
{"step": "error", "progress": 0, "message": "Error description here"}
```

`step` values: `signing`, `installing`, `done`, `error`.

---

## Pages

### `GET /`
Serves `static/index.html` — the single-page web UI.

### `GET /static/{file}`
Static assets (JS, CSS) with `Cache-Control: no-cache` headers.
