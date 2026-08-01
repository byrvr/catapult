# Refresh Reliability and Wake-to-Refresh

Status: approved design, not yet implemented
Date: 2026-08-02
Independent of the sync spec; more urgent than it

## Problem

Auto-refresh is supposed to re-sign and re-install apps inside the 72-hour window before a
free-account provisioning profile expires (`refresh.py:19-24`, `refresh.py:282-342`). In practice
it misses windows, silently retires apps, and actively fights any other machine or tool using the
same Apple ID.

Four defects, all measured or read from source rather than inferred.

### 1. The loop's timer stops while the Mac sleeps

`refresh.py:342` is `await asyncio.sleep(CHECK_INTERVAL)` with `CHECK_INTERVAL = 3600`
(`refresh.py:284`). asyncio's clock is `time.monotonic()`, which on macOS is `mach_absolute_time()`
— and that clock does not advance during system sleep. `mach_continuous_time()` does.

Measured on the author's Mac: `mach_continuous_time` 3107.88 h (129.5 days, matching uptime)
versus `mach_absolute_time` 2049.48 h. **44.1 days uncounted — the loop loses 34% of wall-clock
time.** A laptop that sleeps nightly checks far less often than hourly, and every wake-scheduling
idea below is a no-op until this is fixed.

Fix: compute a wall-clock deadline and sleep toward it, re-checking after each wake, rather than
sleeping a fixed monotonic duration.

### 2. Every refresh revokes every certificate

`get_or_create_cert()` (`developer.py:244-342`) is a misnomer. It never reuses: `developer.py:266`
calls `_revoke_all_certs()` and then generates a fresh RSA-2048 key that is never persisted
(`developer.py:269-271`).

Free-account signing certificates are valid for a **year**, not seven days — verified on the
author's Mac: `Apple Development: …(6DXVW7FY58)`, OU `E7AUFR7897`, `notBefore Jul 17 2026`,
`notAfter Jul 17 2027`. Only the *provisioning profile* carries the 7-day clock. Catapult
destroys a year-long credential hourly for no reason.

The consequence is worse than waste. Any second refresher — another Mac, Xcode, AltStore,
SideStore — has its certificate revoked, and the apps it installed stop launching. This is the
exact ping-pong in altstoreio/AltStore#1597.

Fix: persist certificate and private key as a p12, reuse while Apple still lists the serial, and
revoke only behind explicit user confirmation. Design for **one usable certificate slot** — the
real per-account limit is not authoritatively documented (SideStore's error string implies 1, the
commonly repeated figure is 2, no Apple source exists).

### 3. Three failures retires an app permanently

`MAX_CONSECUTIVE_FAILURES = 3` (`refresh.py:258`), checked in `due_installs` (`refresh.py:269-279`)
and incremented at `refresh.py:443-450`. Three consecutive ticks with the device off the network —
a weekend away — permanently retires the record until a manual reinstall resets it
(`refresh.py:128`).

Fix: exponential backoff instead of permanent retirement.

### 4. Expiry is guessed, not read

`refresh.py:19-24` and `:55-61` compute expiry as `last_installed + 7 days`. A repo-wide grep for
`ExpirationDate` returns nothing. Meanwhile `signer.py:124-149` already CMS-decodes the
provisioning profile — the real `ExpirationDate` is right there.

This matters because the 7-day clock starts at profile **issuance**, not at install, so
`last_installed + 7d` is optimistic by however long signing and installation took, plus any delay
between the two.

Fix: read `ExpirationDate` out of the decoded profile and store it.

**Cheapest confirming test, two minutes, not yet run by anyone:**

```bash
security cms -D -i <app>/embedded.mobileprovision | plutil -p - | grep -E 'CreationDate|ExpirationDate'
```

Nobody in either research pass has actually observed a free-team profile's date pair. Run this
before implementing, and store whatever field it reveals.

## Wake-to-refresh

Once the clock is fixed, the loop can be woken deliberately.

- A `PreventUserIdleSystemSleep` `IOPMAssertion` held across each refresh cycle. No entitlement
  required, and valid on battery.
- An `NSWorkspace.didWakeNotification` observer that kicks the loop immediately on wake rather
  than waiting for the next tick.
- Opt-in `pmset repeat wake`, installed behind a single admin prompt. **`pmset schedule` requires
  root** — verified: as a non-root user it returns "This operation must be run as root". The
  backend is a `gui/<uid>` LaunchAgent (`BackendManager.swift:344-389`), so this needs the same
  privileged-helper treatment as the tunnel prompt.

### What this does and does not deliver

It answers "my Macs are **asleep**", not "my Macs are **off**". Be honest about that in both the
UI and the docs.

The boundary is **plugged in, not lid open**. From `xnu` `IOPMrootDomain.cpp:4473-4486`,
`shouldSleepOnRTCAlarmWake()` returns `!acAdaptorConnected && !clamshellSleepDisableMask`, and
`:8886` calls `privateSleepSystem(kIOPMSleepReasonClamshell)` unconditionally on RTC wake with the
lid closed. The only override needs `com.apple.private.iokit.assertonlidclose`, an Apple-internal
entitlement an ad-hoc-signed DMG can never carry.

Reading the same code the other way gives the supported matrix:

| Configuration | Wake-to-refresh |
|---|---|
| Any desktop Mac | Works — `if (!clamshellExists) return false` |
| MacBook on AC, lid open or closed | Works — `acAdaptorConnected` makes the test false |
| MacBook on battery, lid open | Works |
| MacBook on battery, lid closed | **Dead at the kernel level** |

Measured on the author's Mac: 218 battery dark-wakes with a 2-second median linger (p90 19 s)
versus 82 AC wakes with a 45-second median (p90 3603 s). `caffeinate -s` / `PreventSystemSleep`
is AC-only by default per its own man page.

Given the 72-hour window, "plugged in overnight every third night" is sufficient in practice.
The UI copy should say **"Catapult wakes your Mac to refresh, if it is plugged in or is a
desktop"** — not "refreshes while your Mac sleeps".

## Two loose ends worth fixing in the same pass

**The unattended path can fire an interactive Trust dialog.** `device.py:1609` calls
`create_using_tcp(host, identifier=udid, port=port)`, but `TcpLockdownClient.__init__` does
`self.hostname = hostname` then `self.identifier = hostname` (`lockdown.py:1499-1500`), silently
discarding the UDID. The pair-record lookup then searches for a record named after the IP address,
finds nothing, `validate_pairing` fails, and the default `autopair=True` pops a Trust dialog —
inside the background refresh loop. Fix: pass `pair_record=<plist dict>` explicitly, as
pymobiledevice3's own `get_mobdev2_lockdowns` does at `lockdown.py:1816`.

**The pin is three majors behind.** `pyproject.toml:7` asks for `pymobiledevice3>=4.0.0` and
`uv.lock:1027` resolves to `9.9.0`. A grep for `userspace` across the vendored package returns
zero hits. The userspace, root-free tunnel landed in v10.0.0 (2026-07-22), and that pin is the
direct cause of the `osascript` "with administrator privileges" dialog at `device.py:1039-1045` —
another password prompt fired from inside the refresh loop. Bump to `>=10.0.0` and retest tvOS
pairing.

## Apple ID safety

Ranked by strength of evidence, since a scheduled loop amplifies all of these:

1. **Shared/public anisette servers are the documented account-lock vector.** SideStore's own
   docs state that many users on one v1 server "trips Apple's security, and locks the accounts
   that were using that machine"; issue #708 has a member reporting `-20751` locks "every couple
   of months" even on the official server. Catapult's native AOSKit path avoids this — keep it.
2. **Hourly `_revoke_all_certs` is a revocation storm** on the account. Defect 2 above.
3. **Free-tier rate limits will bite a scheduled loop.** 10 App IDs per 7 days, 3 test devices per
   platform, 3 active apps. Catapult registers an App ID per app **and per `.appex`** on every
   refresh (`refresh.py:386`, `:403-415`). Two machines racing will exhaust the quota, and
   SideStore #410 shows "You may only register 10 App IDs every 7 days" as an unrecoverable
   user-facing failure. **Reuse existing App IDs instead of re-registering per cycle.**
4. Churning anisette device identities registers a new "virtual Mac" on the account each time.
5. Datacenter source IP alone: no evidence found either way. The evidenced risk is synthetic or
   shared device identity, not IP.

## Silent re-authentication

`refresh.py:296-312` abandons the whole cycle when the session is not authenticated, and there is
no re-auth path. `apple_auth.py:111` never stores the password.

This was reported by four separate research agents as a hard architectural blocker and it is not.
Nobody refreshes `gs_token` — the standard design re-mints it by re-running SRP with a stored
password, which AltStore has shipped for years (`BackgroundRefreshAppsOperation.swift:119` runs
with `presentingViewController: nil`, and AltSign throws `requiresTwoFactorAuthentication` only if
a code is genuinely needed). Apple's documentation states a trusted device is re-challenged only
on sign-out, erase, or password change — all user-initiated, none on a timer.

2FA binds to the anisette **machine identity**, not to a renewable token: `X-Apple-MD` rotates on
exact 30-second boundaries while `X-Apple-MD-M` never changed across 130 s of sampling. So a new
machine costs one 2FA prompt, once, then is trusted indefinitely.

Treat this as opt-in, since it means persisting the Apple ID password in the Keychain. Design the
error path for "it 401s, log in again" rather than for a token TTL — no implementation anywhere
tracks `gs_token` expiry.

## Explicitly not building

- **A Linux LAN agent** (Raspberry Pi / NAS / Docker). Every component exists — zsign v1.1.1,
  `anisette-v3-server`, pymobiledevice3 on Linux — but it means rewriting `signer.py` (~430 lines
  of `codesign`/`security`), `anisette.py`, and `device.py`'s launchd/lsof/osascript machinery.
  Two to four weeks plus permanent support burden, to replace a $60 used Mac mini that runs the
  existing DMG unchanged. It also requires storing the Apple ID password as a plaintext-equivalent
  file, since there is no macOS Keychain, on exactly the class of device people expose to the
  internet and never patch.
- **A cloud worker, split or pure.** Signing is the immovable part: `signer.py` shells out to
  `codesign`, and `anisette.py` mints a fresh ~30-second `X-Apple-I-MD` from AOSKit per request.
  "Cloud" therefore means "rent a Mac and hand it your Apple ID." Note the correction that
  installation is *not* the blocker — `device.py:1605-1611` already installs over plain TCP to
  `lockdownd:62078`, `installation_proxy` never needed the iOS 17 tunnel, and pair records are
  portable (`lockdown.py:740`). JitStreamer-EB reached users' iPhones over WireGuard from a public
  server. The phone dials out; NAT is not the obstacle. But JitStreamer-EB is archived, and the
  live ecosystem moved on-device.
- **Pre-signed batches.** Dead twice: the profile clock starts at issuance, and `codesign` seals
  `embedded.mobileprovision` into `_CodeSignature/CodeResources` `files2` with `^.* = true`, so a
  fresh profile cannot be swapped into an already-signed bundle. A credential-free "dumb installer"
  cannot exist.
- **A Catapult iOS companion.** That is SideStore — AGPL-3.0, 4-8 months of work, and it would
  fight SideStore for the account's single free certificate and one of its three app slots.

## Documentation

State plainly in `docs/auto-refresh.md` that Catapult does not do untethered refresh; that
SideStore does for iPhone/iPad; and that genuine 24/7 coverage means leaving any Mac on the LAN
running Catapult — which is also the only option that covers **Apple TV**, since no tvOS loopback
VPN exists and SideStore/StikDebug/AltStore have zero tvOS support.

If pairing-file export is ever added as a SideStore handoff, the UI must say in plain words that a
lockdown pair record contains `HostPrivateKey`, `HostCertificate`, `RootPrivateKey`, and an
`EscrowBag` granting access while the device is locked — anyone holding it plus a route to the
device can install apps, browse app containers over AFC, and read the camera roll.

## Files that change

| File | Change |
|---|---|
| `catapult/refresh.py` | `:284,342` wall-clock deadline; `:258,278,443-450` backoff; `:19-24,55-61` real `ExpirationDate`; `:386,403-415` reuse App IDs |
| `catapult/developer.py` | `:264-266,218-242` stop unconditional revoke; persist and reuse p12 |
| `catapult/power.py` | **New.** `IOPMAssertion` wrapper |
| `catapult/device.py` | `:1609` pass `pair_record` explicitly |
| `native/…/BackendManager.swift` | `~:344-389` wake observer, privileged `pmset` install |
| `pyproject.toml`, `uv.lock` | pymobiledevice3 `>=10.0.0` |
| `docs/auto-refresh.md` | Honest framing, untethered-refresh section |

## Suggested order

1. Wall-clock deadline. Everything else is theatre until this lands.
2. Run the `embedded.mobileprovision` date test; store the real expiry.
3. Certificate reuse. Biggest correctness win, and unblocks two machines coexisting.
4. Backoff, then App ID reuse.
5. `pair_record` fix and the pymobiledevice3 bump — removes two interactive prompts from an
   unattended path.
6. Wake-to-refresh, with the honest UI copy.
7. Silent re-auth, opt-in, last.

## Open questions

- Does `pmset repeat poweron` work from a full shutdown on Apple silicon? Untested, unsettled
  from source. With FileVault on it halts at the login window, where a `gui/<uid>` LaunchAgent
  never starts — so "Mac is off" may be unsolvable regardless.
- Will a locked iPhone with the screen off reliably answer on `lockdownd:62078` and complete an
  install? If not, every architecture degrades to "works only when someone is holding the phone",
  which reorders everything. Test: leave the phone locked on Wi-Fi for 8 hours, then install from
  another machine.
- Is `EnableWifiConnections` actually required before `lockdownd` answers on 62078 over the
  network? pymobiledevice3's docs list it as optional; JitStreamer never mentioned it. Five-minute
  bench test.
- Does Catapult's Apple TV code genuinely lose remote-pairing trust after ~7 days, or is that error
  text stale? It comes from `device.py:825-834` — Catapult's own message, not Apple's. If stale,
  tvOS unattended refresh is meaningfully less human-gated than assumed.
