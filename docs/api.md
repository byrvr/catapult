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

## File Endpoints

### `POST /api/upload`
Upload an IPA file (multipart form data, field name `file`).

**Response:**
```json
{
  "path": "/tmp/catapult_uploads/app.ipa",
  "info": {
    "bundle_id": "com.example.app",
    "bundle_name": "My App",
    "version": "2.1.0",
    "build": "42",
    "min_os": "16.0",
    "executable": "MyApp"
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
  "ipa_path": "/tmp/catapult_uploads/app.ipa"
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
