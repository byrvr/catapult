# Apple ID Authentication

Catapult authenticates using Apple's **GSA (Grand Slam Authentication)** protocol with SRP-6a, the same path used by macOS, Xcode, and AltServer. The standard `idmsa.apple.com` web flow is blocked for non-browser TLS clients.

## Endpoint

```
POST https://gsa.apple.com/grandslam/GsService2
Content-Type: text/x-xml-plist
```

All requests are plist-encoded and include Anisette headers and a consistent device identity.

## Three-Phase Flow

### Phase 1 — SRP Init

Client sends its SRP public key `A2k` (2048-bit) to Apple.

```
Client → Apple:
  A2k:      <SRP client public key>
  u:        apple_id (email)
  ps:       [ "s2k", "s2k_fo" ]  (supported password derivation schemes)
  cpd:      { ...anisette headers... }

Apple → Client:
  sp:       "s2k" or "s2k_fo"
  s:        <salt bytes>
  B:        <server SRP public key>
  i:        <PBKDF2 iterations>
  c:        <server cookie>
```

### Phase 2 — SRP Complete

Client derives the password hash and computes proof `M`.

**Password derivation** (`s2k` scheme):
```python
p = sha256(password.encode("utf-8")).digest()
# For s2k_fo: p = sha256(password).hexdigest().encode("utf-8")
derived = PBKDF2-HMAC-SHA256(password=p, salt=s, iterations=i, dklen=32)
```

**SRP parameters**: RFC 5054, NG_2048 group, no username in X.

**CRITICAL**: The SRP private key `a` from Phase 1 **must be reused** in Phase 2. Generating a new random `a` produces a different `A` and the server proof `M2` cannot be verified.

```python
# Save from Phase 1
saved_a = usr.a

# Restore in Phase 2 before computing M
usr.a = saved_a
usr.A = pow(usr.g, saved_a, usr.N)
M = usr.process_challenge(salt, B)
```

```
Client → Apple:
  M1:   <SRP client proof>
  c:    <cookie from Phase 1>
  cpd:  { ...anisette headers... }

Apple → Client:
  M2:   <SRP server proof>   (verify this!)
  spd:  <encrypted session data blob>
  np:   <new padding>
```

**spd decryption** (AES-256-CBC):
```python
key = HMAC-SHA256(session_key, b"extra data key:")
iv  = HMAC-SHA256(session_key, b"extra data iv:")[:16]
plaintext = AES-256-CBC-decrypt(spd, key, iv)
# Apple omits PKCS7 padding sometimes — try unpadding, ignore error
```

The decrypted spd contains a bare `<dict>...</dict>` plist (no `<?xml?>` wrapper). Wrap before parsing:
```python
raw = b"<?xml version='1.0'?><plist version='1.0'>" + spd + b"</plist>"
data = plistlib.loads(raw)
```

Key fields extracted from spd:
- `adsid` — Account DSid
- `GsIdmsToken` — Token for 2FA triggers
- `sk` — 32-byte session key
- `c` — 168-byte cookie for Phase 3
- `DsPrsId` — Data & Privacy record ID

### Phase 3 — App Token Exchange

Exchange the session key and cookie for an app-specific token used by Developer Services.

**Checksum** (HMAC-SHA256):
```python
checksum = HMAC-SHA256(
    key = sk,
    msg = b"XYZ" + apple_id.encode() + b"APPLE"
)
```

```
Client → Apple:
  u:        apple_id
  app:      ["com.apple.gs.xcode.auth"]
  c:        <cookie from spd>
  t:        <GsIdmsToken>
  checksum: <HMAC above>
  cpd:      { ...anisette headers... }

Apple → Client:
  t:  { "com.apple.gs.xcode.auth": { "token": <et>, "exp": ... } }
```

**Token decryption** (AES-256-GCM):

The `et` field format is:
```
[3 bytes "XYZ"] [16 bytes IV] [N bytes ciphertext] [16 bytes GCM tag]
```

```python
tag        = et[-16:]
ciphertext = et[19:-16]
iv         = et[3:19]
plaintext  = AES-256-GCM-decrypt(key=sk, nonce=iv, data=ciphertext, tag=tag)
gs_token   = plaintext.decode()
```

## 2FA Flow

If Phase 2 returns `ec=5000` with auth type `trustedDeviceSecondaryAuth` or `secondaryAuth`:

1. **Trigger push** to trusted devices:
   ```
   POST https://gsa.apple.com/auth/verify/trusteddevice
   Authorization: Bearer {identity_token}
   X-Apple-Widget-Key: 83545bf919730e51dbfba24e7e8a78d2
   ```
   where `identity_token = base64(adsid + ":" + GsIdmsToken)`

2. **User enters 6-digit code** from trusted device.

3. **Validate**:
   ```
   POST https://gsa.apple.com/auth/verify/trusteddevice/securitycode
   Authorization: Bearer {identity_token}
   security-code: {"code": "123456"}
   ```

4. On success (`200 OK`), re-run Phases 1–3 with the now-validated session.

## Headers

All GSA requests include:
```
User-Agent: akd/1.0 CFNetwork/1568.200.51 Darwin/24.1.0
X-MMe-Client-Info: <MacBookPro18,3> <Mac OS X;13.4.1;22F8> <com.apple.AOSKit/282 (com.apple.dt.Xcode/3594.4.19)>
```

## Error Codes

| `ec` | Meaning |
|------|---------|
| `0` | Success |
| `5000` | Wrong credentials or 2FA required |
| Other | Apple-specific error |
