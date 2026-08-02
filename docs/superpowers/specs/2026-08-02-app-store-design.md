# App Store — Sources, Catalog, and Daily Auto-Update

Status: approved design
Date: 2026-08-02

## Problem

Installing an app means finding an `.ipa` by hand, downloading it, picking it in
Catapult, and repeating all of that whenever the developer ships a new build.
For a project like [VortX](https://github.com/VortXTV/VortX/releases), which
cuts releases every few days, that is constant manual work.

## Goal

Add sources once. Catapult lists what they publish, installs the right build for
the selected device, and keeps it current.

## Sources

Two kinds, normalised into one catalog.

**GitHub** — `owner/repo`. Catapult polls
`GET /repos/{owner}/{repo}/releases` and reads `.ipa` assets. No auth needed for
public repos; the unauthenticated rate limit is 60 requests/hour per IP, which a
daily check never approaches.

**AltStore** — a URL to an `apps.json` in the AltStore/SideStore source format.
Gives icons, screenshots, descriptions, and changelogs for free, and makes the
existing source ecosystem usable.

Catapult ships **no default catalog**. The user adds sources. This keeps it a
tool rather than a distribution channel.

Stored at `~/Library/Application Support/Catapult/sources.json`:

```json
{
  "sources": [
    {
      "id": "github:VortXTV/VortX",
      "kind": "github",
      "url": "https://github.com/VortXTV/VortX",
      "include_prerelease": false
    }
  ]
}
```

## Asset matching

One VortX release publishes four IPAs plus unrelated artefacts:

```
VortX-iOS-v0.3.14-beta.12-ci.ipa          63.3 MB
VortX-tvOS-v0.3.14-beta.12-ci.ipa         62.9 MB
VortX-tvOS-lite-v0.3.14-beta.12-ci.ipa    36.2 MB
VortX-Android-…apk / VortX-macOS-…dmg     (ignored)
SHA256SUMS-ci.txt                          (used for verification)
```

Classification is filename-first:

| token in filename | platform |
|---|---|
| `tvos`, `appletv` | `tvos` |
| `ios`, `iphone`, `ipad`, `universal` | `ios` |
| none of the above | `unknown` |

`unknown` entries are still listed but resolved authoritatively from the IPA's
`UIDeviceFamily` after download (`1` = iPhone, `2` = iPad, `3` = tvOS) — the
filename is a hint, the bundle is the truth.

A build variant (`lite` above) becomes a **separate catalog entry**, not a
hidden alternative: they are different apps with different bundle IDs, and the
user picks. Anything between the platform token and the version token is treated
as the variant.

Catapult only offers entries whose platform matches the selected device, so an
Apple TV never shows an iOS build.

## Versions

GitHub's `prerelease` flag is unreliable here — VortX marks `v0.3.14-beta.12`
as `prerelease: false`. So Catapult ignores the flag and compares tags directly,
with an "include pre-release tags" toggle per source that matches on the tag
string (`alpha`, `beta`, `rc`, `dev`, `nightly`, `preview`).

Comparison is a natural-order key, because lexical comparison gets
`beta.9 > beta.12` backwards:

- strip a leading `v`, split into numeric and alphabetic runs
- numeric runs compare as integers
- a tag with a trailing alphabetic run is a **pre-release** of the version
  formed by its numeric prefix, so `0.3.14` outranks `0.3.14-beta.12`

Apps can be pinned to a version, which excludes them from auto-update.

## Integrity

When a release publishes a `SHA256SUMS*` asset, Catapult downloads it and
verifies the IPA before installing. When it does not, the IPA goes into the
content-addressed vault under its own computed digest anyway, so re-downloads
are skipped and sync gets it for free.

## Install

Store installs reuse the existing pipeline rather than duplicating it: download
to a temp file, verify, `vault.store_ipa()`, then the same cert → App ID →
profile → sign → install path as a manual install. That means the bundle-ID
namespacing, App Store conflict avoidance, and framework identifier fixes all
apply unchanged.

## Auto-update

Folded into the existing refresh loop rather than a second scheduler. That loop
already has wall-clock timing, exponential backoff, certificate reuse, and the
power assertion.

- Hourly: the existing expiry check.
- Daily (per source, tracked by `last_checked_at`): fetch the catalog, compare
  against installed records, and queue anything newer.
- Per-app opt-in, off by default.
- A queued update installs only when its device is reachable. Nothing installs
  to a device that is not there.

Honest framing for the UI: **checks daily, installs when the device is
connected.** For iPhone and iPad that means plugged in, or on Wi-Fi once paired.

## Quota

A free Apple team allows 10 App ID registrations per 7 days and 3 installed
apps — a store exhausts that almost immediately. The Store tab shows a warning
when the selected team is free. Paid teams get 100 App IDs and year-long
profiles, so auto-update is cheap there.

## Files

| File | Change |
|---|---|
| `catapult/store.py` | **New.** Sources, adapters, catalog, matching, versions |
| `catapult/server.py` | Store endpoints; install reuses the existing flow |
| `catapult/refresh.py` | Daily source check inside the existing loop |
| `native/…/StoreView.swift` | **New.** Store tab |
| `native/…/Models.swift` | Source and catalog models |
| `native/…/APIClient.swift` | Store methods |

## Out of scope

Screenshots and review UI, paid or authenticated sources, source signing/trust,
and any bundled default catalog.
