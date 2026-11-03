#!/usr/bin/env bash
# Prepare the embedded bootstrap Python shipped inside the installer.
# It is copied under <install>/engine/bootstrap and is the ONLY interpreter the
# app may use to create the optional AI runtime venv. The system Python is never used.
set -euo pipefail

DESTINATION="${1:-}"
CACHE_ROOT="${2:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -n "$CACHE_ROOT" ] || CACHE_ROOT="$ROOT/.local/cache/bootstrap-python"
[ -n "$DESTINATION" ] || DESTINATION="$ROOT/apps/desktop/src-tauri/engine/bootstrap"

OS="$(uname -s)"
MACHINE="$(uname -m)"
case "$OS/$MACHINE" in
  Darwin/arm64)
    ASSET='cpython-3.11.18+20261026-aarch64-apple-darwin-install_only.tar.gz'
    SHA='cba3455e18516fbe8668cff2820c9b6a53447c552d612443c2b2fb60d390a908'
    URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-aarch64-apple-darwin-install_only.tar.gz'
    KEY='darwin-arm64'
    ;;
  Darwin/x86_64)
    ASSET='cpython-3.11.18+20261026-x86_64-apple-darwin-install_only.tar.gz'
    SHA='a85474bcc78a98ca6b0070d8d615b0494b912b960239e6f18cc12d648f2dc270'
    URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-x86_64-apple-darwin-install_only.tar.gz'
    KEY='darwin-x64'
    ;;
  Linux/x86_64)
    ASSET='cpython-3.11.18+20261026-x86_64-unknown-linux-gnu-install_only.tar.gz'
    SHA='a87ee63646c8f8438b7af3954b2135ed2af91d033c631f12a8f20af903046d06'
    URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz'
    KEY='linux-x64'
    ;;
  Linux/aarch64|Linux/arm64)
    ASSET='cpython-3.11.18+20261026-aarch64-unknown-linux-gnu-install_only.tar.gz'
    SHA='a344c887b04a309f292cac1ef7c7a85e39e11932ec301d635258c569d830861b'
    URL='https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.11.18%2B20261026-aarch64-unknown-linux-gnu-install_only.tar.gz'
    KEY='linux-arm64'
    ;;
  *) echo "unsupported bootstrap platform: $OS/$MACHINE" >&2; exit 2 ;;
esac

WORK="$CACHE_ROOT/$KEY"
mkdir -p "$WORK"
if [ ! -x "$WORK/python/bin/python3" ]; then
  ARCHIVE="$WORK/$ASSET"
  if [ ! -f "$ARCHIVE" ]; then
    echo "[bootstrap] download $ASSET"
    curl -fL --retry 3 -o "$ARCHIVE" "$URL"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
  else
    ACTUAL="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
  fi
  [ "$ACTUAL" = "$SHA" ] || { echo "bootstrap python checksum mismatch" >&2; exit 1; }
  rm -rf "$WORK/python"
  tar -xzf "$ARCHIVE" -C "$WORK"
fi
GETPIP="$WORK/get-pip.py"
if [ ! -f "$GETPIP" ]; then
  echo '[bootstrap] download get-pip.py'
  curl -fL --retry 3 -o "$GETPIP" 'https://bootstrap.pypa.io/get-pip.py'
fi
rm -rf "$DESTINATION"
mkdir -p "$DESTINATION"
cp -R "$WORK/python" "$DESTINATION/python"
cp "$GETPIP" "$DESTINATION/get-pip.py"
SIZE="$(du -sk "$DESTINATION" | awk '{print $1}')"
echo "[bootstrap] embedded Python ready: $DESTINATION (${SIZE} KB)"
