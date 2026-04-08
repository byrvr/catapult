# Apple Developer Services API

Catapult uses Apple's internal provisioning API (the same one used by Xcode and AltServer) to create development certificates and provisioning profiles without a paid developer account.

## Endpoint

```
POST https://developerservices2.apple.com/services/QH65B2/{endpoint}?clientId=XABBG36SBA
Content-Type: text/x-xml-plist
```

All requests are plist-encoded and return plist responses.

## Authentication Headers

```
X-Apple-I-Identity-Id: {adsid}           # Raw account DSid
X-Apple-GS-Token: {gs_token}             # App-specific token from Phase 3
X-Apple-I-MD: {otp}                       # Anisette OTP
X-Apple-I-MD-M: {otp_m}                   # Anisette OTP (secondary)
X-Apple-I-MD-RINFO: 17106176
X-Mme-Device-Id: {device_id}
X-Apple-I-MD-LU: {local_user_id}
X-Apple-I-Client-Time: {timestamp}
X-MMe-Client-Info: <MacBookPro18,3> ...
```

**Note**: The header is `X-Apple-I-Identity-Id` (raw adsid), NOT `X-Apple-Identity-Token` (base64), and NOT cookie-based `myacinfo`. The `X-Apple-GS-Token` is the Xcode app token from Phase 3 auth, not the session key.

## Common Request Structure

```python
{
    "clientId": "XABBG36SBA",
    "protocolVersion": "QH65B2",
    "requestId": str(uuid.uuid4()).upper(),
    "teamId": team_id,
    # ... endpoint-specific fields
}
```

## Result Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `35` | Already exists (idempotent — treated as success) |
| `9401` | Bundle ID unavailable |
| Other | Error |

## Provisioning Flow

### 1. Fetch Team — `listTeams.action`

**Note**: This endpoint has **no `ios/` prefix** — it's the only one.

```
POST /services/QH65B2/listTeams.action
```

Returns list of teams. Catapult prefers the first `Active` team with type `Individual` (free developer account).

### 2. Certificate Management

#### List — `ios/listAllDevelopmentCerts.action`
Returns all development certificates for the team.

#### Revoke — `ios/revokeDevelopmentCert.action`
```python
{
    "teamId": team_id,
    "serialNumber": cert_serial,    # NOT certificateId
}
```

Free accounts have a certificate limit (typically 2). Catapult revokes all existing certs before creating a new one.

#### Create — `ios/submitDevelopmentCSR.action`
```python
{
    "teamId": team_id,
    "csrContent": pem_string,
    "machineId": str(uuid.uuid4()).upper(),
    "machineName": "Catapult",
}
```

The CSR response **does not include** the certificate content — only a `certificateId`. A follow-up `listAllDevelopmentCerts` is required to fetch the `certContent` PEM.

Certificate is RSA-2048 with CN=`Catapult`.

### 3. Device Registration — `ios/addDevice.action`

```python
{
    "teamId": team_id,
    "deviceNumber": real_udid,      # Must be actual hardware UDID from RSD
    "name": device_name,
}
```

**CRITICAL**: The UDID must be the real hardware UDID from `rsd.udid` (e.g., `00008110-000C65D91EF1801E`), not the mDNS service name or tunneld pairing UUID. Apple rejects UUID-format identifiers.

Result code 35 ("already exists") is treated as success.

### 4. App ID Registration — `ios/addAppId.action`

Catapult registers a **sideload-namespaced** bundle ID to avoid conflicts with App Store apps:

```
com.catapult.{TEAM_ID}.{safe_original_bundle_id}
```

Example: `com.stremio.ios` → `com.catapult.E7AUFR7897.com-stremio-ios`

```python
{
    "teamId": team_id,
    "identifier": sideload_bundle_id,
    "name": f"Catapult {bundle_name}",
}
```

On rc=35 or rc=9401 (bundle taken), Catapult looks up the existing app ID via `ios/listAppIds.action` and returns it.

### 5. Provisioning Profile — `ios/downloadTeamProvisioningProfile.action`

**Why this endpoint, not `createProvisioningProfile`?**

`downloadTeamProvisioningProfile` asks Apple to **generate a fresh profile server-side** that automatically includes all registered certificates and devices for the team. It requires only `appIdId` — no explicit lists of cert IDs or device IDs.

```python
{
    "teamId": team_id,
    "appIdId": app_id_id,
    "subPlatform": "tvOS",      # Required for Apple TV targets
}
```

Before calling this, Catapult deletes all stale profiles for the app ID via `ios/deleteProvisioningProfile.action`. Stale profiles contain old certificates and would cause installation failures.

The response contains `provisioningProfile.encodedProfile` — the binary `.mobileprovision` bytes.

**For tvOS**: `subPlatform: "tvOS"` must be included, otherwise Apple returns an iOS profile that fails verification on Apple TV (`0xe8008015`).

## Error Handling

```python
class DeveloperServicesError(Exception):
    def __init__(self, message, result_code=None):
        self.result_code = result_code
```

The `result_code` field allows callers to distinguish "already exists" (35) from real errors without string matching.
