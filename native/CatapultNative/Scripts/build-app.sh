#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PACKAGE_DIR}/../.." && pwd)"

APP_NAME="Catapult"
APP_DIR="${REPO_ROOT}/dist/${APP_NAME}.app"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
BACKEND_DIR="${RESOURCES_DIR}/backend"

cd "${PACKAGE_DIR}"
swift build -c release

rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}" "${BACKEND_DIR}"

cp "${PACKAGE_DIR}/.build/release/CatapultNative" "${MACOS_DIR}/${APP_NAME}"
chmod +x "${MACOS_DIR}/${APP_NAME}"

UV_SOURCE="${CATAPULT_UV:-}"
if [[ -z "${UV_SOURCE}" ]]; then
  UV_SOURCE="$(command -v uv || true)"
fi
for candidate in "${HOME}/.local/bin/uv" "${HOME}/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
  if [[ -z "${UV_SOURCE}" && -x "${candidate}" ]]; then
    UV_SOURCE="${candidate}"
  fi
done
if [[ -z "${UV_SOURCE}" || ! -x "${UV_SOURCE}" ]]; then
  echo "error: uv not found. Install uv or set CATAPULT_UV=/path/to/uv." >&2
  exit 1
fi
cp "${UV_SOURCE}" "${RESOURCES_DIR}/uv"
chmod +x "${RESOURCES_DIR}/uv"

rsync -a --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'build' \
  --exclude 'dist' \
  --exclude 'native/CatapultNative/.build' \
  "${REPO_ROOT}/catapult/" "${BACKEND_DIR}/catapult/"

rsync -a --delete "${REPO_ROOT}/static/" "${BACKEND_DIR}/static/"
rsync -a --delete "${REPO_ROOT}/docs/" "${BACKEND_DIR}/docs/"
cp "${REPO_ROOT}/run.py" "${BACKEND_DIR}/run.py"
cp "${REPO_ROOT}/pyproject.toml" "${BACKEND_DIR}/pyproject.toml"
cp "${REPO_ROOT}/uv.lock" "${BACKEND_DIR}/uv.lock"

if [[ -f "${REPO_ROOT}/Catapult.icns" ]]; then
  cp "${REPO_ROOT}/Catapult.icns" "${RESOURCES_DIR}/Catapult.icns"
fi
for asset in CatapultIcon.png CatapultMenuBarTemplate.png; do
  if [[ -f "${REPO_ROOT}/${asset}" ]]; then
    cp "${REPO_ROOT}/${asset}" "${RESOURCES_DIR}/${asset}"
  fi
done

cat > "${CONTENTS_DIR}/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key>
    <string>Catapult</string>
    <key>CFBundleIdentifier</key>
    <string>com.catapult.native</string>
    <key>CFBundleName</key>
    <string>Catapult</string>
    <key>CFBundleDisplayName</key>
    <string>Catapult</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>0.3.3</string>
    <key>CFBundleVersion</key>
    <string>0.3.3</string>
    <key>CFBundleIconFile</key>
    <string>Catapult</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSLocalNetworkUsageDescription</key>
    <string>Catapult needs local network access to discover and communicate with iOS and tvOS devices.</string>
    <key>NSBonjourServices</key>
    <array>
        <string>_apple-mobdev2._tcp</string>
        <string>_remotepairing._tcp</string>
        <string>_remotepairing-manual-pairing._tcp</string>
        <string>_companion-link._tcp</string>
        <string>_airplay._tcp</string>
    </array>
</dict>
</plist>
PLIST

echo "Built ${APP_DIR}"
