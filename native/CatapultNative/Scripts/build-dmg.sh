#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKAGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${PACKAGE_DIR}/../.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"

APP_NAME="Catapult"
VERSION="0.3.8"
VOLUME_NAME="${APP_NAME}"
OUTPUT_DIR="${WORKSPACE_ROOT}/outputs"
APP_SOURCE="${REPO_ROOT}/dist/${APP_NAME}.app"
APP_OUTPUT="${OUTPUT_DIR}/${APP_NAME}.app"
ZIP_OUTPUT="${OUTPUT_DIR}/${APP_NAME}.app.zip"
DMG_OUTPUT="${OUTPUT_DIR}/${APP_NAME}-${VERSION}.dmg"
BACKGROUND_OUTPUT="${REPO_ROOT}/dist/dmg-background.png"
SYNC_SETUP="${CATAPULT_DMG_SYNC_SETUP:-}"
ENCRYPTED_SYNC="${CATAPULT_DMG_ENCRYPTED_SYNC:-}"

swift "${SCRIPT_DIR}/generate-icons.swift"
swift "${SCRIPT_DIR}/generate-dmg-background.swift"
"${SCRIPT_DIR}/build-app.sh"
codesign --force --deep --sign - "${APP_SOURCE}"

mkdir -p "${OUTPUT_DIR}"
rm -rf "${APP_OUTPUT}" "${ZIP_OUTPUT}" "${DMG_OUTPUT}"

ditto "${APP_SOURCE}" "${APP_OUTPUT}"
ditto -c -k --keepParent "${APP_OUTPUT}" "${ZIP_OUTPUT}"

uvx --from dmgbuild dmgbuild \
  "${VOLUME_NAME}" \
  "${DMG_OUTPUT}" \
  --settings "${SCRIPT_DIR}/dmg-settings.py" \
  -Dapp="${APP_SOURCE}" \
  -Dbackground="${BACKGROUND_OUTPUT}" \
  -Dsync_setup="${SYNC_SETUP}" \
  -Dencrypted_sync="${ENCRYPTED_SYNC}" \
  --detach-retries 10

echo "Built ${APP_OUTPUT}"
echo "Built ${ZIP_OUTPUT}"
echo "Built ${DMG_OUTPUT}"
