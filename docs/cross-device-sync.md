# Cross-Device Sync

Catapult needs more than an Apple ID login to refresh an app from a different
Mac. Apple can say which App IDs and profiles exist, and a reachable device can
say what is installed, but neither gives back the original `.ipa` bytes needed
for re-signing.

So Catapult keeps an encrypted IPA vault plus a manifest in storage the user
already owns.

## Goals

- A second Mac signs in with the same Apple ID, enters one recovery key, and
  recovers previous installs.
- Catapult never uploads Apple ID passwords, auth tokens, signing keys, or
  plaintext IPAs.
- Per-user isolation is structural, not something Catapult's code has to
  enforce: the vault is in the user's own cloud account.
- Missing device, missing IPA, locked vault, and wrong key are explicit states
  rather than vague failures.

## Where the vault lives

The default is the user's own **iCloud Drive**:

```text
~/Library/Mobile Documents/com~apple~CloudDocs/Catapult/
```

This is ordinary file I/O from a non-sandboxed process. It needs no
entitlement, no Team ID, no provisioning profile, and no notarization — which
matters, because Catapult is ad-hoc signed and an ad-hoc-signed bundle
declaring any non-allowlisted entitlement is killed at launch. That also rules
out CloudKit, ubiquity containers, and iCloud Keychain sync.

Alternatives:

- **Any already-synced folder** — Dropbox, Google Drive, OneDrive, Syncthing.
  Their desktop clients handle the account, so Catapult inherits per-user
  isolation with no auth code. Settings → Sync → "Choose a folder…".
- **Cloudflare R2** or any S3-compatible bucket, for people who want one.
  There is no Settings UI for it yet: configure it with
  `POST /api/sync/configure` (`provider: "r2"` plus the bucket details and,
  for anything other than R2, a `region`) or the legacy environment variables.
  Blobs stream to and from the bucket a megabyte at a time.

Sync is **off** until you turn it on. Writing IPAs into someone's iCloud Drive
uninvited would quietly consume their 5 GB free tier, which is shared with
Photos, Mail, and device backups.

## Layout

```text
teams/<team_id>/vault.json              # plaintext: the wrapped data key
teams/<team_id>/manifest.json.enc
teams/<team_id>/ipas/<sha256>.ipa.enc
```

The manifest holds only operational metadata: Apple ID and team, device UDID
and name, bundle IDs, IPA hash/size/filename, install and expiry times, and
refresh status.

## Encryption

Envelope encryption:

```
DK  = 32 random bytes          # encrypts the manifest and every blob
RK  = 16 random bytes          # the recovery key you carry between Macs
KEK = HKDF-SHA256(RK, salt=team_id, info="catapult/recovery/v1")
vault.json = AES-256-GCM(KEK).encrypt(DK, aad=team_id)
```

`vault.json` is plaintext and carries no secret — only `DK` sealed under `KEK`.
So a second Mac needs exactly one thing: the recovery key.

There is deliberately **no password KDF**. `RK` is uniformly random, so the
offline attack floor is already 2^128; stretching a generated key buys nothing
and would add a tuning problem across the old Intel Macs macOS 14 still
supports. This is the reasoning behind 1Password's unstretched Secret Key.

The AEAD tag on the wrapped key is the wrong-key check. Unwrapping fails in
about a millisecond, so a mistyped key is reported immediately rather than
after a 500 MB download.

`team_id` is both the HKDF salt and the AEAD associated data, binding a vault
to its team.

### The recovery key

Crockford Base32 with the mod-37 check symbol:

```
CAT1-5THEW-Z0A23-MREVR-WN87F-BMBEG-CP
```

Chosen over Bech32 and BIP-39 because it *decodes* `O`→`0` and `I`/`L`→`1`, is
case-insensitive, and ignores grouping — including in the `CAT1` prefix, since
someone retyping from a printout will write `CATI`. The check symbol catches a
single-character typo before any network call.

Catapult caches the key in the login Keychain after a successful unlock. That
is a **cache**: its absence means "ask for the key", never "make a new one".

## Vault states

| Keychain | Remote `vault.json` | State |
|---|---|---|
| empty | absent | `needs_setup` — offer to create |
| empty | present | `locked` — ask for the recovery key |
| present | present, opens | `ok` |
| present | present, will not open | `wrong_key` |

The `locked` row is the whole point. Previously an empty Keychain caused
Catapult to mint a fresh random key, so a second Mac silently created an
incompatible vault and uploaded into it.

## Setting it up

**Mac #1** — Settings → Sync → pick iCloud Drive (or a folder) → Create vault.
The recovery key is shown once, with Copy and Save-to-file, behind an "I saved
it" checkbox.

**Mac #2** — install Catapult, sign into the same Apple ID, open Settings →
Sync. It finds the vault and shows a paste field. Press ⌘V: Universal Clipboard
has usually already carried the key across, since both Macs are on the same
Apple Account.

**If you lose the key**, choose "Start a new vault instead" and confirm.
Catapult moves the old vault aside (`teams/<team_id>.replaced-<timestamp>`),
mints a new key, and re-uploads from this Mac on the next sync; other Macs need
the new key. Your IPAs still exist
locally in `~/Library/Application Support/Catapult/IPAs`, so losing the key is
annoying rather than fatal.

## Blob format

Blobs are content-addressed by SHA-256 and encrypted in 4 MiB chunks
(`catapult-sync-v2`), each sealed with a per-file nonce plus a counter. Each
chunk's associated data carries its index and a final-chunk flag, so truncation
and reordering are detected instead of producing a short IPA.

The previous format encrypted whole blobs in memory — roughly 3 GB of RSS for a
1 GB IPA. Old `catapult-sync-v1` blobs still decrypt, so existing vaults keep
working with no migration; they age out as apps are re-added.

Writes are staged **outside** the synced folder and moved in atomically.
Staging inside it meant the sync client saw a partial multi-hundred-megabyte
temp file and pushed it to every other Mac before it was renamed away.

## Migrating from the old scheme

Earlier versions read `CATAPULT_SYNC_PROVIDER`, `CATAPULT_SYNC_KEY`, and the R2
variables from the environment or `~/.catapult/config.env`. A Finder-launched
app inherits no shell environment, which is why that never really worked.

Settings now live in `~/Library/Application Support/Catapult/sync.json`, with
R2 credentials in the Keychain. The old values are still read for one release
as a fallback while `sync.json` is absent: `CATAPULT_SYNC_KEY` is adopted
**verbatim** as the data key and wrapped under a freshly generated recovery
key, so every already-uploaded blob stays readable and nothing is re-encrypted
or re-uploaded. The new recovery key lands in this Mac's Keychain; open
Settings → Sync → "Show recovery key…" to save it, then delete
`~/.catapult/config.env`. A second Mac that still has the same
`CATAPULT_SYNC_KEY` keeps opening the vault without it.

The "personal encrypted DMG" mechanism, which embedded R2 credentials in the
disk image, has been removed. It was one mis-set environment variable away from
publishing working bucket keys to GitHub Releases.

## Concurrent Macs

Two Macs sharing a vault must not refresh the same app at once. Before each
hourly refresh cycle a Mac takes a lease at `teams/<team_id>/lease.json`
(`locked_by` is a random per-Mac id, `locked_until` is 20 minutes out,
`operation` is `refresh`) and releases it afterwards. A Mac that finds a live
lease held by another machine skips that cycle and tries again next hour; a
Mac that dies mid-cycle frees the other one when the lease expires. A synced
folder has no atomic compare-and-set, so the lease is advisory: it removes the
common case of two hourly loops lining up, not every race.

The lease alone is not sufficient — certificate reuse is the other half, since
a refresh used to revoke every certificate on the account and break whatever
the other Mac had installed. See [Auto-refresh](auto-refresh.md).
