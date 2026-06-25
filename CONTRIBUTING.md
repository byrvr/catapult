# Contributing

Catapult is a native macOS app with a local Python backend. The most useful
contributions are focused fixes around device discovery, pairing, signing,
install progress, refresh reliability, and clear error reporting.

## Development Setup

```bash
git clone https://github.com/byrvr/catapult.git
cd catapult
uv sync
cd native/CatapultNative
swift run
```

Build a release-style DMG from the repository root:

```bash
./native/CatapultNative/Scripts/build-dmg.sh
```

## Before Opening A PR

- Run `python3 -m py_compile catapult/*.py`.
- Run `swift build -c release` from `native/CatapultNative`.
- Keep public builds free of Apple ID credentials, signing keys, R2 keys, sync
  keys, IPA files, provisioning profiles, and personal device identifiers.
- Update `CHANGELOG.md` for user-visible behavior changes.

## Project Boundaries

Catapult should stay local-first. Apple ID sessions, signing material, and
plaintext IPAs must remain on the user's Mac unless the user explicitly enables
encrypted sync.
