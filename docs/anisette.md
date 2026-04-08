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

### 3. Error

If neither source is available, `AnisetteError` is raised with instructions.

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
