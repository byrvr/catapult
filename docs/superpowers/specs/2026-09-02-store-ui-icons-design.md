# Store UI: richer rows and app icons

## Goal

Make the Store tab scannable at a glance: each entry shows a real app icon
where one can be had, a name and publisher line, one line of meta, and status
as compact pills. Add a search field. Keep the list layout, the sources pane,
the All / Installed / New switch, the install progress strip, and the install
pipeline as they are.

## Rows

A row is the 44pt icon plus 12pt padding, on the existing card background:

```
[icon 44pt]  YouTubePlus   (Installed 21.24.3) (Update available) (pre-release)
             mrdrvt99 · 21.24.3-5.2.2 · 128,9 MB                      [Install]
                                                             ☐ Update automatically
```

- **Name line:** app name in `callout.weight(.semibold)`, followed by pills on
  the same line, wrapping to the meta line only if the window is narrow.
- **Meta line:** `developer · version · size`, with the platform label added
  after the version only when the platform is known (`Apple TV`,
  `iPhone & iPad`). "Unknown platform" is never shown.
- **Pills** (all `caption2`, capsule, 6/2 padding), in this order, only when
  they apply: `Installed <version>` green tint; `Update available` orange
  tint; `Installed before on <devices>` secondary tint (only when not
  installed on the selected device); `pre-release` orange outline.
- **Action:** the existing Install / Update / Reinstall button on the right,
  same enablement rules. For an app installed on the selected device, the
  "Update automatically" checkbox sits directly under the button, right
  aligned, `caption2`.

## Header

Title and subtitle on the left as today. On the right: a search field
(`magnifyingglass`, placeholder "Search apps", 200pt) that filters by name or
developer as you type, case-insensitive; then the All / Installed / New
segmented switch; then the Sources and Refresh buttons. The filter empty state
covers "no search hits" with the text "No apps match “<query>”."

## Icons

The backend resolves one `icon` string per catalog entry, in this order:

1. The source's own icon URL (AltStore `iconURL`).
2. A locally extracted icon, served as `/api/store/icon?sha=<ipa sha256>`,
   when an IPA for this entry is already on this Mac: any install record the
   entry matches (`match_strength`), or the Store download cache
   `~/.catapult/downloads/<first 16 hex of sha256(download_url)>.ipa`.
3. The GitHub owner's avatar, `https://github.com/<owner>.png?size=128`, for
   GitHub sources.
4. `""`, and the app draws a monogram tile.

### Extraction (`catapult/store.py`)

`icon_from_ipa(path) -> bytes | None` opens the IPA as a zip and first looks
for loose PNGs whose name starts with `AppIcon` at the `.app` root
(`Payload/<name>.app/AppIcon60x60@2x.png`, not inside `PlugIns/`, `Watch/`
or frameworks), returning the largest by IHDR pixel width, file size breaking
ties.

Modern apps ship no loose icon PNGs: the icon lives only in the compiled
`Assets.car`, as an "Icon Image" asset whose name the Info.plist gives under
`CFBundleIcons.CFBundlePrimaryIcon.CFBundleIconName` (YouTube's is
`logo_youtube_2024_q4_color`). When no loose PNG exists, `icon_from_ipa`
reads the candidate names from the plist — `CFBundleIconName` from
`CFBundleIcons` and `CFBundleIcons~ipad`, then each `CFBundleIconFiles` entry
with a trailing `<w>x<h>` size stripped, then `AppIcon` — extracts
`Assets.car` to a temporary directory, and runs the helper
`catapult-icon <Assets.car> <name> <out.png>` for each name until one
succeeds. The helper is found through `CATAPULT_ICON_HELPER`, then the dev
checkout's `native/CatapultNative/.build/release/catapult-icon`, then
`/Applications/Catapult.app/Contents/MacOS/catapult-icon`; with no helper the
result is `None` and the row falls back to the avatar.

### Helper (`native/CatapultNative/Sources/CatapultIcon/main.swift`)

A second executable product of the Swift package, `catapult-icon`, copied
into `Contents/MacOS/` by `build-app.sh`. It reads the catalog with macOS's
own CoreUI framework (`CUICatalog`, loaded at runtime), asks
`iconImageWithName:scaleFactor:deviceIdiom:…:desiredSize:` for the idioms
marketing, phone, pad, tv and universal at sizes 1024 down to 60, keeps the
largest bitmap, falls back to `imageWithName:…` for image-set icons, and
writes a PNG. Exit 0 on success, 1 when the catalog has no such icon, 2 on
bad arguments, 3 when the catalog cannot be opened. The app passes the
helper's path to the backend as `CATAPULT_ICON_HELPER`, both for the
development backend it spawns and in the login agent's environment.

`cached_icon(ipa_path, sha256) -> Path | None` memoizes on disk under
`~/Library/Application Support/Catapult/icons/`: `<sha>.png` on success, an
empty `<sha>.none` marker on failure, so a 130 MB IPA is scanned once.

`owner_avatar_url(source) -> str` returns the avatar URL for GitHub sources
and `""` otherwise.

### API

- `GET /api/store/apps` gains `icon` per entry (string, possibly empty).
- `GET /api/store/icon?sha=<64 hex>` serves the cached PNG with
  `Cache-Control: max-age=86400`; `404` for an unknown or malformed sha.

### Client (`StoreView.swift`, `Models.swift`)

`StoreApp.icon: String?`. `StoreAppIcon` renders: an `AsyncImage` for a
non-empty `icon` (an absolute URL, or a backend-relative path resolved against
the API client's base URL), clipped to a rounded rectangle with corner radius
`size * 0.23`; while loading and on failure or an empty `icon`, a monogram
tile: the first letters of up to two words, white on a hue derived from the
name's hash, same rounded rectangle.

## Testing

- `icon_from_ipa`: picks the largest of two `AppIcon*.png` files by IHDR
  width; ignores PNGs under `PlugIns/`; returns `None` with no icons; returns
  `None` for a non-zip; with only an `Assets.car`, runs the helper with the
  plist's icon name first (a fake helper script in the tests records its
  arguments and writes a fixed PNG) and returns `None` when no helper exists.
- `cached_icon`: writes `<sha>.png` once and reuses it; writes the `.none`
  marker and does not rescan.
- `owner_avatar_url`: GitHub and AltStore sources.
- `/api/store/apps`: the four-step resolution order, one test per step.
- `/api/store/icon`: serves a cached file, `404` on unknown and on `sha=../x`.
- Swift: `swift build -c release`, then a relaunch of the app.

## Out of scope

Grouping by source, a changelog disclosure, moving the sources pane, tvOS
layered icons (the helper handles flat icon assets only), and icon caching on
the Swift side beyond what `AsyncImage` does.
