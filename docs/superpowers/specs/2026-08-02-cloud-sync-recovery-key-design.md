# Universal Cross-Device Sync — iCloud Drive Vault + Recovery Key

Status: approved design, not yet implemented
Date: 2026-08-02
Supersedes the env-var configuration model in `docs/cross-device-sync.md`

## Problem

Cross-device sync exists in the code but is unusable by anyone who is not the author.

Configuration comes only from environment variables or a plaintext `~/.catapult/config.env`
(`sync.py:37-93`). A Finder-launched app inherits no shell environment, so the dotfile is
load-bearing and undocumented in the UI. To turn sync on, a user must own a Cloudflare R2
bucket, copy four secrets plus a shared key, and repeat it identically on every Mac.
There is no settings surface at all — Swift renders a read-only status row (`Views.swift:713-770`).

Underneath the configuration problem are three real defects:

1. **`get_sync_key()` mints a key instead of reporting a locked vault.** `sync.py:130-149`
   generates a fresh random key when the Keychain has none. Mac #2 therefore does not say
   "I need your key" — it silently creates a second, incompatible vault and uploads into it.
   This is the defect that makes sync feel broken.
2. **`_normalize_key()` falls back to bare unsalted `sha256(passphrase)`** (`sync.py:109-123`).
   No KDF, no stretching, no salt.
3. **Whole-blob encryption holds the entire IPA in memory.** `sync.py:424` does
   `_encrypt_bytes(key, local_ipa.read_bytes())` — roughly 3 GB peak RSS for a 1 GB IPA.

## Goal

Anyone who downloads the public DMG can turn on sync, and recover on a second Mac, without a
terminal, without editing a dotfile, and without creating an account with a third-party
infrastructure vendor.

## Decisions

### Storage: the user's own iCloud Drive folder

Default vault root is `~/Library/Mobile Documents/com~apple~CloudDocs/Catapult/`.

This is ordinary POSIX file I/O from a non-sandboxed process. It requires no entitlement, no
Team ID, no provisioning profile, and no notarization — which matters because the app is
ad-hoc signed (`build-dmg.sh:24`), and an ad-hoc-signed bundle declaring any non-allowlisted
entitlement is SIGKILLed at launch (exit 137, verified empirically, including with an
arbitrary made-up entitlement as a control). Per-user isolation is structural: it is the
user's own iCloud account, so there is no shared bucket, no credential shipped in the DMG,
no developer-side storage bill, and no hosting liability.

It also reuses `FolderStore` (`sync.py:189-211`) unchanged in principle. The default is a
different `Path`, not a new provider.

**Rejected, with reasons:**

- *CloudKit, ubiquity containers, iCloud Keychain sync* — hard-dead under ad-hoc signing, not
  degraded. `kSecAttrSynchronizable` returns `-34018 errSecMissingEntitlement`.
- *Dropbox / Google Drive OAuth* — requires the user to create a third-party account, which
  violates the goal. Dropbox Basic is 2 GB, which does not fit one large IPA.
- *Developer-hosted vault* — cost is not the objection (~$34/month at 5000 users on R2). The
  objection is that the developer becomes account holder for other people's encrypted iOS
  binaries, and one notice against that account breaks every user's vault at once.
- *Buying the $99 Apple Developer Program for this feature* — it unlocks nothing this design
  needs, and a revoked Developer ID means the app cannot launch even where already installed,
  which hands Apple a switch that kills the whole sideloader. Notarization remains worth buying
  on its own merits (Gatekeeper, stable TCC identity across releases). Do not buy it *for* sync.

### Fallback: any already-synced folder

An `NSOpenPanel` directory picker, pre-seeded with shortcuts detected from
`~/Library/CloudStorage/*`. Same `FolderStore`, same `vault.json`, different root. Covers users
with iCloud Drive off, users past the 5 GB free tier, and users who prefer Google's 15 GB.

Known limitation to state in the UI, not to engineer around: Google Drive streamed files are
only readable while Drive for desktop is running, and Dropbox creates
`manifest (Mac's conflicted copy).json.enc` on concurrent writes, which Catapult will never open.

### Advanced: keep R2

`R2Store` stays, with its configuration moved from env vars into the settings pane and the
Keychain. It is not the default because it requires a third-party infrastructure account.
Add the missing `region` field to `SyncConfig` — currently hardcoded to `"auto"` in
`R2Store.__init__` (`sync.py:220`), which is correct only for R2 and wrong for B2/Wasabi/iDrive.

### Unlock: a generated Recovery Key, envelope encryption, no KDF

```
DK  = secrets.token_bytes(32)          # encrypts the manifest and every IPA blob
RK  = secrets.token_bytes(16)          # 128 bits, generated, never user-chosen
KEK = HKDF-SHA256(ikm=RK, salt=team_id_bytes, info=b"catapult/recovery/v1", length=32)
ct  = AESGCM(KEK).encrypt(nonce12, DK, aad=team_id)
```

The wrapped blob (~400 bytes) is written as **plaintext JSON** to
`teams/<team_id>/vault.json`, alongside the existing `manifest.json.enc`:

```json
{
  "vault_format": 2,
  "wrap": { "alg": "hkdf-sha256+aes256gcm", "nonce": "<b64>", "ct": "<b64>" }
}
```

No Argon2id, no scrypt, deliberately. `RK` is uniformly random, so the offline attack floor is
already 2^128. A slow KDF over a generated key buys nothing and introduces a parameter-tuning
problem across the old dual-core Intel Macs that macOS 14 still supports. Same reasoning as
1Password's unstretched Secret Key.

The AES-GCM tag on the wrapped blob **is** the wrong-key verifier. Do not add a separate
`sha256(KEK)` check — it adds no usability and gives an attacker a cheaper oracle than the AEAD.
Unwrap fails in ~1 ms, so Mac #2 reports "that Recovery Key doesn't match this vault" instantly
rather than after a 500 MB download.

Envelope encryption also means the passphrase can later be changed without re-encrypting blobs.

**Human format:** Crockford Base32 with the mod-37 check symbol. 128 bits = 26 characters plus
one check character, in groups of 5, with a non-secret version prefix:

```
CAT1-K7X2M-9QZ4B-T3VHN-8RJ5W-DGP6Y-F
```

Crockford is chosen over Bech32 and BIP-39 because it *decodes* `O`→`0` and `I`/`l`→`1`, is
case-insensitive, and ignores stray hyphens. For a stressed user retyping from a printout that
is worth more than Bech32's stronger checksum. Roughly 80 lines in a new `catapult/recoverykey.py`
with no new dependencies.

**Keychain is a cache, not a source of truth.** After a successful unwrap, cache `RK` in the login
Keychain via the existing `_keychain_get`/`_keychain_set` (`refresh.py:166-189`), keyed by team ID.
The current key-minting behavior at `sync.py:130-149` must be **deleted** and replaced with:

| Keychain | Remote `vault.json` | State |
|---|---|---|
| empty | exists | `locked` — prompt for the Recovery Key |
| empty | absent | `needs_setup` — offer to create a new vault |
| present | exists | `ok` |
| present | mismatched | `wrong_key` |

### Encryption stays, even though the storage is user-owned

The manifest carries Apple ID, team ID, and device UDIDs, and plaintext IPAs sitting legibly in
iCloud is exactly the artifact that should not be associated with the user's Apple account.
That is the argument for E2E here — not confidentiality from the developer, who never sees the
bytes anyway.

## User experience

**Mac #1 — three interactions.**

1. Settings → Sync → iCloud Drive → Enable. macOS shows the one-time Files-and-Folders consent
   prompt. Allow.
2. A modal: "Save your Recovery Key. Catapult cannot recover this for you." Large monospace key,
   `[Copy]`, `[Save Recovery Kit…]` (a plain `.txt` the user can AirDrop), `[Print]`, and a
   gating "I saved my Recovery Key" checkbox.
3. Done. Nothing is ever asked again on this Mac.

**Mac #2 — one keystroke.**

1. Install the same DMG, sign into the same Apple Account (already required by Catapult).
2. Settings → Sync → iCloud Drive. Catapult finds `vault.json` already replicated and shows
   "This vault is locked."
3. One large pre-focused paste field. Press ⌘V — Universal Clipboard already carried the key
   from Mac #1's Copy button, since both Macs are on the same Apple Account. Alternates:
   `[Open Recovery Kit file…]`, or type it.
4. Input is Crockford-normalized before parsing; the check symbol catches a typo before any
   network call.

**Default for a user who never opens settings: sync is OFF.** Do not auto-enable. Silently
writing IPAs into someone's iCloud Drive and consuming their 5 GB free tier is hostile, and
`~/Library/Mobile Documents` may not exist at all — it does not on the author's Mac. The default
state is one row in the main window: "Cross-device sync: Off — Set up…". If
`~/Library/Mobile Documents/com~apple~CloudDocs` is absent, the pane shows an inline
"Turn on iCloud Drive in System Settings" deep link instead of a dead radio button.

**Lost-key path ships in the same release.** Settings → Sync → "Start a new vault" mints a fresh
`DK`+`RK` and re-uploads from the local vault at `~/Library/Application Support/Catapult/IPAs`.
This is safe precisely because the plaintext IPAs still exist on at least one Mac — losing the
key is annoying, not catastrophic. Say so in plain language on the Mac #1 modal.

## Storage and quota

Surface vault size against the free tier in the status row: "412 MB of 5 GB iCloud used by
Catapult." iCloud's free 5 GB is shared with Photos, Mail, and device backups, so quota is the
constraint most likely to bite in practice. Treat "out of quota" as a first-class status, not
an exception.

**Eviction.** With "Optimize Mac Storage" on, macOS can turn a 500 MB encrypted IPA into an APFS
dataless file, and a cold read then blocks on a download that can take minutes or fail — inside
a 7-day re-sign deadline. Apple removed every CLI hydration tool (`brctl download` and
`brctl evict` in Sonoma 14, `fileproviderctl materialize` in 14.4). Mitigation is two-part:
set the `com.apple.fileprovider.pinned` xattr on the vault (what Finder's "Keep Downloaded"
uses, undocumented), and pre-hydrate 24 h before expiry rather than at expiry, using
`FileManager.startDownloadingUbiquitousItem(at:)` and polling `URLResourceKey`.

Progress and completion are readable without entitlements via `ubiquitousItemIsUploadedKey`,
`ubiquitousItemPercentUploadedKey`, `ubiquitousItemDownloadingStatusKey`, and friends. Poll from
Swift and report over the existing local API. Do not build on `brctl` — `brctl status` already
fails with `BRCloudDocsErrorDomain Code=141 "Access denied"` on the author's Mac.

## Refresh leases

Two Macs sharing a vault currently fight: `developer.py:264-266` revokes every certificate on
each refresh, so whichever machine runs last invalidates the other's apps. The lease described
in `docs/cross-device-sync.md` but never implemented is therefore required, not optional:

```json
{ "locked_by": "<machine-id>", "locked_until": 1780000000.0, "operation": "refresh" }
```

Written to `teams/<team_id>/lease.json`. Acquire before a refresh cycle, honor a short TTL, and
skip the cycle if another machine holds it.

The lease alone does not fix the fight — certificate reuse does, and that lands in the separate
refresh-reliability spec. Both are needed.

## Blob format v2 — streaming

`sync.py:424` is replaced. Add `put_file(key, path)` / `get_file(key, path)` to the `RemoteStore`
ABC (`sync.py:178-186`) so blobs stream through disk instead of RAM.

New blobs are written as `catapult-sync-v2`: 4 MiB chunks, per-chunk nonce derived from a random
file nonce plus a counter, and an explicit final-chunk marker so truncation is detectable. Keep
reading `catapult-sync-v1` (`sync.py:162-175`) forever for existing objects. Because blobs are
content-addressed, v1 objects simply age out as apps are re-added — no migration, no re-upload.

`FolderStore` must write its temp file **outside** the synced root. The current code creates
`<sha>.ipa.enc.tmp` inside the target directory (`sync.py:205-208`), which means a partial 500 MB
temp file gets uploaded to iCloud and pushed to every other Mac before being renamed away. Watch
for `EXDEV` on the cross-mount rename — a File Provider domain is its own mount point; on failure,
fall back to a temp inside the root named with a leading dot and a `.part` suffix.

*Decision made without an explicit ruling: v2 is included here rather than deferred, because the
3 GB peak RSS is inside the sync code path and iCloud Drive is exactly where the large blobs go.
Say if you want it split out.*

## Migration

Non-breaking, one release of overlap, zero forced re-upload.

1. Remote layout is unchanged. `teams/<team_id>/manifest.json.enc` and
   `teams/<team_id>/ipas/<sha256>.ipa.enc` stay at those keys (`sync.py:320-325`). Only
   `vault.json` and `lease.json` are added. Existing R2 and folder vaults keep working.
2. On first run of the new build, if `CATAPULT_SYNC_KEY` is set: run `_normalize_key()` on it
   exactly as today, adopt the result as `DK` verbatim, generate a fresh `RK`, wrap, and PUT
   `vault.json`. Show a one-time sheet: "Sync now uses a Recovery Key. Here it is — save it.
   You can delete `~/.catapult/config.env`." Every already-uploaded blob stays readable because
   `DK` is unchanged.
3. Config moves to `~/Library/Application Support/Catapult/sync.json` (provider + folder path;
   R2 credentials to the login Keychain). Keep `_parse_config_env()` and `_sync_setting()` as a
   **read-only** fallback for exactly one release, imported on first run, then deleted. This is
   also what fixes "Finder-launched apps don't inherit shell env": Swift passes resolved config
   to the backend.
4. Delete the personal-encrypted-DMG mechanism — `SYNC_SETUP` / `ENCRYPTED_SYNC` in
   `Scripts/build-dmg.sh:18-19,38-39` and the `sync_setup`/`encrypted_sync` handling in
   `Scripts/dmg-settings.py:3-14,31`. Shipping the developer's own R2 keys inside a DMG is a
   credential-disclosure incident waiting to happen. Rewrite `docs/cross-device-sync.md`.

## API changes

`/api/sync/status` (`server.py:359`) grows `vault_state` ∈ `{disabled, needs_setup, needs_icloud,
locked, ok, wrong_key, quota_warning}` and `vault_bytes`. The existing `status`/`needs_key`/
`wrong_key` strings and `portable_key` stay for one release so `Views.swift:736-760` keeps
compiling.

New: `POST /api/sync/configure` (provider + folder), `POST /api/sync/unlock` (recovery key),
`GET /api/sync/recovery-key` (show-once, valid only immediately after creation).
`/api/sync/run` (`server.py:373`) is unchanged.

## Files that change

| File | Change |
|---|---|
| `catapult/sync.py` | Bulk of the work: key handling, streaming, v2 format, states, lease |
| `catapult/recoverykey.py` | **New.** Crockford Base32 + check symbol, ~80 lines |
| `catapult/server.py` | 3 new endpoints, `vault_state` in status |
| `catapult/vault.py` | Unchanged — already content-addressed |
| `native/…/SyncSettingsView.swift` | **New.** Provider choice, recovery-key modal, unlock field |
| `native/…/Views.swift` | `:713-770` status row becomes tappable, opens the pane |
| `native/…/APIClient.swift` | +4 methods |
| `native/…/AppState.swift` | +sync state |
| `native/…/CatapultNativeApp.swift` | `:9` add a Settings scene beside the existing Window |
| `Scripts/build-dmg.sh`, `Scripts/dmg-settings.py` | Remove encrypted-DMG path |
| `docs/cross-device-sync.md`, `docs/api.md` | Rewrite |

## Blocking prerequisite — a 30-minute smoke test

**Nobody has verified that an ad-hoc-signed app's Python child process can read and write iCloud
Drive.** iCloud Drive is off on the author's Mac and `~/Library/Mobile Documents` does not exist,
which is why every research pass inferred rather than tested. The entire design rests on this.

Enable iCloud Drive on a Mac and confirm:

1. Does the Files-and-Folders TCC prompt fire, and what does it name — Catapult, or `python`?
2. Does the `uv`-spawned Python backend inherit the app's grant, or prompt separately / return `EPERM`?
3. Does a file written by Python actually replicate to a second Mac?

Related correction worth carrying: an earlier research pass budgeted "port all vault I/O from
Python to Swift." That was a category error. Entitlements bind to a process's own main executable,
but this design uses **no entitlement** — only a TCC grant, which is attributed to the responsible
process and inherited by children. Do not do that rewrite. Confirm inheritance in the smoke test.

Second, smaller unknown: `FileManager.ubiquityIdentityToken` was measured as nil on a Mac with
iCloud Drive **off**, which proves nothing about entitlements. Apple's documentation attributes a
nil token solely to account state and recommends it as the launch-time availability check. If it
works entitlement-free, the settings pane gets a clean "is iCloud usable" probe instead of
stat'ing a path. Retest with iCloud Drive on.

## Out of scope

Optional passphrase wrap, key rotation, BIP-39 rendering, QR display, camera QR capture,
Bonjour/PAKE LAN pairing, and iCloud Keychain sync. The last is impossible under ad-hoc signing
regardless.

## Open questions

- **Eviction policy:** pin the vault (guaranteeing it occupies real local disk) or accept stalls
  with an honest progress UI? Leaning pin + pre-hydrate, but it is a disk-space tradeoff the user
  arguably should control.
- **Vault size cap:** free accounts permit 3 concurrently installed sideloaded apps, so a live
  vault is ~3 IPAs, but the manifest accumulates historical installs forever. Explicit
  prune-oldest policy, or just surface the number and let users delete?
- **Keep R2 at all?** Retaining it costs ~1 day. Deleting it is defensible under YAGNI but
  strands whoever is already on the env-var scheme.
