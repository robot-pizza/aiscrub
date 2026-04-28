#!/usr/bin/env bash
# Build a local PyInstaller binary and zip it. Mirrors the release workflow.
set -euo pipefail

PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"

case "$PLATFORM" in
  linux)  ASSET="aiscrub-linux-${ARCH}" ;;
  darwin) ASSET="aiscrub-macos-${ARCH}" ;;
  msys*|mingw*|cygwin*) ASSET="aiscrub-windows-${ARCH}" ;;
  *) ASSET="aiscrub-${PLATFORM}-${ARCH}" ;;
esac

uv sync --extra build
uv run pyinstaller --onefile --name aiscrub --console aiscrub.py

rm -rf staging
mkdir -p staging
if [ -f dist/aiscrub.exe ]; then
  cp dist/aiscrub.exe "staging/aiscrub.exe"
else
  cp dist/aiscrub "staging/aiscrub"
  chmod +x "staging/aiscrub"
fi
cp README.md LICENSE staging/ 2>/dev/null || true

rm -f "${ASSET}.zip"
( cd staging && zip -r "../${ASSET}.zip" . )

echo
echo "built: ${ASSET}.zip"
