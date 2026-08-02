# Azərbaycan IPTV

Free-to-air Azerbaijani TV channels as a public HLS playlist, in both JSON and
M3U form, for loading into a TV app.

```
playlist.json     # full channel list with tiers, alternates, headers, logos, EPG
playlist.m3u      # only the verified channels (12) - safe anywhere
playlist-all.m3u  # verified + the AZ-geo-locked ones (29) - use this inside Azerbaijan
selftest.py     # re-verify every channel from your own network
build_m3u.py    # regenerate playlist.m3u from playlist.json
```

**Raw URLs** (what you point your TV at):

```
https://raw.githubusercontent.com/byrvr/catapult/claude/parallel-agents-azeri-streams-az8hu0/azeri-iptv/playlist-all.m3u
https://raw.githubusercontent.com/byrvr/catapult/claude/parallel-agents-azeri-streams-az8hu0/azeri-iptv/playlist.m3u
https://raw.githubusercontent.com/byrvr/catapult/claude/parallel-agents-azeri-streams-az8hu0/azeri-iptv/playlist.json
```

## Tiers, and why they exist

Streams were checked from a **US** vantage point, so the results split three ways.

| Tier | Count | Meaning |
|---|---|---|
| `verified` | 12 | Confirmed working end-to-end, repeatedly, from outside Azerbaijan |
| `degraded` | 1 | Plays, but fails the quality bar — see `warnings` on the entry |
| `az_only` | 17 | Official platform URL that returns HTTP 403 outside Azerbaijan |

The `az_only` channels — ATV, ARB, ARB 24, ARB Günəş, Space, Real, İctimai,
İdman, Qafqaz, MTV, VIP, SH TV, TMB, Show Plus, Biznes, Kanal S, CBC — are
**not broken**. They are the real URLs the national `yoda.az` platform uses,
and they are geo-restricted. This was tested rather than assumed: a valid
session token was minted for the test IP and every gated channel still
returned 403, which rules out an auth problem and leaves geo/licensing.

**On an Azerbaijani connection most of these should simply work**, which is why
they ship in `playlist.json` with their real URLs instead of being dropped. They
are held out of `playlist.m3u` so your TV isn't fed 17 entries that may spin and
fail. Run the self-test to find out which ones work for you, then promote them:

```bash
python3 selftest.py --tier az_only     # see what your network can reach
python3 selftest.py --promote          # move passing channels into `verified`
python3 build_m3u.py                   # rebuild the .m3u with them included
```

## What "verified" actually means

A stream counts as working only if **all** of these hold. The final URL set passed
**4 full re-checks over ~35 minutes**; individual URLs were checked 7–11 times
across the whole run:

1. The manifest returns 200 and parses as HLS, not an HTML error page
2. A media playlist resolves (via the highest-bandwidth variant, for masters)
3. It is live — no `#EXT-X-ENDLIST`
4. A real segment downloads with meaningful bytes, at measured throughput
5. **The live edge advances on re-fetch** — the media sequence moves forward

Step 5 is the one that matters most. A dead channel's manifest often keeps
returning 200 long after the encoder stops, so "the URL responds" is not
evidence that anything is playing. Two subtleties were worth handling:

- Some origins **load-balance across backends with independent sequence
  numbering** (~1.1M on one, ~11.0M on another), so a naive before/after
  comparison reads as "went backwards" on a perfectly healthy channel. The
  check clusters samples by magnitude and requires progress within a cluster.
- Sampling the *first* segment is unreliable — on a short DVR window it can roll
  off between fetching the playlist and fetching the segment, 404ing on a
  healthy stream. The middle segment is sampled instead.

Observed stability, counting every check run against each URL:

| Result | Channels |
|---|---|
| clean on every pass | Ayaz TV, AzStar, GunAz, KN Music, Naxçıvan, Vilayət, Xəzər, Mədəniyyət, Baku TV, CBC |
| one transient blip | AzTV (alt edge only — primary held), APA TV (503) |

## Notes on individual channels

- **CBC** needs `Referer: https://player.castr.com/`. It's in the M3U as an
  `#EXTVLCOPT` line; players that ignore those will get a 403.
- **APA TV** is HTTP-only (no TLS) and returned one transient 503 during testing.
- **EL TV** (`degraded`) has an **expired TLS certificate** — most TV players
  will refuse it outright. Kept in the JSON for completeness, excluded from the M3U.
- **AzTV, Baku TV, Naxçıvan, AzStar** each have a second independent edge in `alt`.
  `selftest.py` falls back to those automatically.
- **GunAz TV** and **Vilayət TV** are South Azerbaijani (Iranian Azeri) channels;
  **AzStar** is diaspora, broadcast from Canada. Labelled in the `note` field.
- **Kanal 35** appears in several public playlists tagged `group-title="Azerbaijan"`.
  It is an **İzmir regional channel**, not Azerbaijani, and is excluded.

## Sources

Candidates came from the [iptv-org](https://github.com/iptv-org/iptv) playlists
and API, the official broadcaster sites (aztv.az, medeniyyettv.az, xezerxeber.az,
baku.tv, cbctv.az, idmantv.az, itv.az, atv.az), the `yoda.az` national platform
config, and public community playlists. Only free-to-air sources are included —
no subscription-portal scrapes and no credential-bearing Xtream URLs.

Widely-copied playlists are largely stale: every one of the 21 URLs in the most
frequently mirrored Azerbaijan playlist is dead, and the `UzunMuhalefet/streams`
indirection that ~12 channels depend on has had its directory deleted.

## Maintenance

Stream URLs rot. Re-run `python3 selftest.py` periodically; it prints a
pass/fail line per channel and demotes anything in `verified` that stops
responding when run with `--promote`.
