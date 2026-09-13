# Anisette Data

Every request to Apple's GSA and Developer Services APIs requires **Anisette headers** — a set of device-identity and one-time-password (OTP) values that Apple uses to detect unusual sign-in activity.

## What It Is

Anisette data consists of two types of values:

**Stable (per-session, generated once):**
| Header | Description |
|--------|-------------|
| `X-Mme-Device-Id` | Random UUID representing this "device" |
| `X-Apple-I-MD-LU` | Base64-encoded random local user ID |
| `X-Apple-I-MD-RINFO` | Fixed: `17106176` |
| `X-Apple-I-SRL-NO` | Fixed: `0` |

**OTP (short-lived, ~30 seconds):**
| Header | Description |
|--------|-------------|
| `X-Apple-I-MD` | One-time machine digest |
| `X-Apple-I-MD-M` | One-time machine digest (secondary) |

## Critical Consistency Requirement

The stable identity values **must be identical** across all requests in a session — GSA SRP init, GSA SRP complete, 2FA trigger, and every Developer Services call.

If `X-Mme-Device-Id` or `X-Apple-I-MD-LU` differ between the SRP complete and the 2FA trigger, Apple returns **401 Unauthorized**.

Catapult generates these once at module load and reuses them for the entire process lifetime:
```python
_DEVICE_ID    = str(uuid.uuid4()).upper()
_LOCAL_USER_ID = base64.b64encode(uuid.uuid4().bytes).decode()
```

## OTP Sources (Priority Order)

### 1. Native macOS (AOSKit) — preferred, no dependencies

Catapult calls directly into Apple's private `AOSKit.framework` using `pyobjc`:

```python
import objc
from Foundation import NSClassFromString, NSBundle

aoskit = NSBundle.bundleWithPath_("/System/Library/PrivateFrameworks/AOSKit.framework")
aoskit.load()
AOSUtilities = NSClassFromString("AOSUtilities")
raw = AOSUtilities.retrieveOTPHeadersForDSID_("-2")
```

**Note**: macOS returns `X-Apple-MD` and `X-Apple-MD-M` (without the `I-` prefix). Catapult normalizes these to `X-Apple-I-MD` and `X-Apple-I-MD-M`.

This works on macOS 13+ without any additional software.

### 2. omnisette-server — Docker fallback

If native macOS fails, Catapult falls back to a local HTTP server:

```
GET http://127.0.0.1:6969
→ {"X-Apple-I-MD": "...", "X-Apple-I-MD-M": "..."}
```

Start with Docker:
```bash
docker run -d -p 6969:80 ghcr.io/sidestore/omnisette-server:latest
```

### 3. SideStore anisette v3 — the only option on macOS 26+

macOS 26 gates AOSKit behind private entitlements: `retrieveOTPHeadersForDSID:`
logs `AOSKit WARN: A: Info request failed: -45070` and returns nothing, so on
macOS 27 the native source never answers. Catapult then speaks the SideStore
anisette v3 protocol to a remote server, `https://ani.sidestore.zip` by default
(`CATAPULT_ANISETTE_SERVER` overrides it, e.g. for a self-hosted
`anisette-v3-server`).

v3 is personal rather than shared: the identity is provisioned once and stored
in `~/.catapult/anisette-v3.json` (mode 0600):

1. `GET {server}/v3/client_info` → the machine description and user agent the
   server's ADI library was set up for.
2. `GET https://gsa.apple.com/grandslam/GsService2/lookup` → the MidService
   `midStartProvisioning` / `midFinishProvisioning` URLs.
3. WebSocket `wss://{server}/v3/provisioning_session`: the server asks for our
   identifier (16 bytes, seeded from this Mac's device id), then for `spim`
   (we POST `startMachine` to Apple), then for `ptm`/`tk` (we POST
   `endMachine` with the server's `cpim`), and finally returns `adi_pb`.
4. From then on `POST {server}/v3/get_headers` with `identifier` + `adi_pb`
   yields `X-Apple-I-MD`, `X-Apple-I-MD-M` and `X-Apple-I-MD-RINFO`. A
   `-45061` reply means the identity expired; Catapult re-provisions once.

With v3 the identity headers come from the provider, exactly as SideStore
derives them: `X-Mme-Device-Id` is the identifier as a UUID (so it stays equal
to the module-level device id) and `X-Apple-I-MD-LU` is its SHA-256 hex.

### X-MMe-Client-Info and the GSA 503

Since September 2026 `gsa.apple.com/grandslam/GsService2` answers **503** to
any `X-MMe-Client-Info` naming `com.apple.dt.Xcode` (AltServer 1.7.6 fixed the
same thing). `anisette.gsa_client_info()` rewrites the identifier to
`com.apple.akd/1.0` in whatever description is presented to GSA, the 2FA
endpoints and developer services — the v3 server's own description included.
Provisioning itself first presents the server's description verbatim and only
falls back to the akd form if GSA refuses it.

### 4. Error

If no source answers, `AnisetteError` names each one and how to point Catapult
at another v3 server.

## Usage in Requests

Two public functions are exported:

**`get_anisette_headers()`** — for GSA SRP requests (plist `cpd` field):
```python
{
    "X-Apple-I-MD": "...",
    "X-Apple-I-MD-M": "...",
    "X-Apple-I-MD-RINFO": "17106176",
    "X-Apple-I-MD-LU": "...",
    "X-Mme-Device-Id": "...",
    "X-Apple-I-SRL-NO": "0",
    "X-Apple-I-Client-Time": "2026-04-08T12:00:00Z",
    "X-Apple-I-TimeZone": "UTC",
    "X-Apple-Locale": "en_US",
    "loc": "en_US",
    # GSA-specific flags:
    "bootstrap": True,
    "icscrec": True,
    "pbe": False,
    "prkgen": True,
    "svct": "iCloud",
}
```

**`get_anisette_http_headers()`** — for 2FA trigger and Developer Services (HTTP headers):

Same as above plus:
```python
"X-MMe-Client-Info": "<MacBookPro18,3> <Mac OS X;13.4.1;22F8> <com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>"
```
