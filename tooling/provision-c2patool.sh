#!/usr/bin/env bash
# Provision c2patool v0.9.12 for BUILD-TIME packaging on macOS/Linux.
#
# Downloads the pinned official release asset, verifies its SHA-256 and copies
# the binary to <repo>/bin/c2patool/c2patool (path expected by the engine).
# Nothing is downloaded at product runtime.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT/bin/c2patool"
DEST="$DEST_DIR/c2patool"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

case "$(uname -s)" in
  Linux)
    URL="https://github.com/contentauth/c2patool/releases/download/v0.9.12/c2patool-v0.9.12-x86_64-unknown-linux-gnu.tar.gz"
    SHA="caa336747d5364e21e0eb7a307c108b6680b237ff8b8aa5ed94ab8e43d64214c"
    ARCHIVE="$WORK/c2patool.tar.gz"
    echo "[c2patool] linux download"
    curl -fsSL "$URL" -o "$ARCHIVE"
    echo "$SHA  $ARCHIVE" | sha256sum -c -
    mkdir -p "$WORK/extract"
    tar -xzf "$ARCHIVE" -C "$WORK/extract"
    ;;
  Darwin)
    URL="https://github.com/contentauth/c2patool/releases/download/v0.9.12/c2patool-v0.9.12-universal-apple-darwin.zip"
    SHA="37284ab259513e9ddc22797a3956af7abad9cb390ed4ff3c6acaa45c350bd255"
    ARCHIVE="$WORK/c2patool.zip"
    echo "[c2patool] macOS download"
    curl -fsSL "$URL" -o "$ARCHIVE"
    echo "$SHA  $ARCHIVE" | shasum -a 256 -c -
    mkdir -p "$WORK/extract"
    unzip -q "$ARCHIVE" -d "$WORK/extract"
    ;;
  *)
    echo "::error::unsupported platform $(uname -s) for c2patool provisioning"
    exit 1
    ;;
esac

BIN=""
while IFS= read -r candidate; do
  BIN="$candidate"
  break
done < <(find "$WORK/extract" -type f -name c2patool)

if [ -z "$BIN" ]; then
  echo "::error::c2patool binary not found in the archive"
  find "$WORK/extract" -maxdepth 3 -type f || true
  exit 1
fi

mkdir -p "$DEST_DIR"
cp -f "$BIN" "$DEST"
chmod +x "$DEST"
file "$DEST" || true
"$DEST" --version
echo "[c2patool] ready: $DEST"
