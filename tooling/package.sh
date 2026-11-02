#!/usr/bin/env bash
# NL VO Shield - package the macOS/Linux (and git-bash Windows) desktop installer(s) into dist/.
#
# Pipeline: leak guard -> frontend build -> engine sidecar -> `tauri build` bundles
# -> dist/<version>/ with stable artifact names, SHA256SUMS, BUILD-INFO.json and a
# binary content scan.
#
# Signing: if TAURI_SIGNING_PRIVATE_KEY is set AND the Tauri config declares an
# updater pubkey, the bundler signs updater artifacts; otherwise artifacts are
# UNSIGNED and auto-update stays disabled (recorded in BUILD-INFO.json).
#
# The base installer ships NO models and no Python runtime from the host; the
# engine payload is produced by tooling/build-engine-sidecar.sh.
#
# Usage:
#   ./tooling/package.sh
#   ./tooling/package.sh --bundles dmg
#   ./tooling/package.sh --skip-leak-guard --skip-sidecar
#
# Exit codes: 0 ok; 1 packaging failure; 2 prerequisite missing; 3 leak guard failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
BUNDLES=""
SKIP_LEAK_GUARD=0
SKIP_SIDECAR=0
SKIP_INSTALL=0
WITH_AUDIOSEAL=0
EXPLICIT_ROOT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --bundles) shift; BUNDLES="${1:-}" ;;
    --skip-leak-guard) SKIP_LEAK_GUARD=1 ;;
    --skip-sidecar) SKIP_SIDECAR=1 ;;
    --skip-install) SKIP_INSTALL=1 ;;
    --with-audioseal) WITH_AUDIOSEAL=1 ;;
    --root) shift; EXPLICIT_ROOT="${1:-}" ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[ERROR] unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }
fail() { log ERROR "$1"; exit "${2:-1}"; }

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/assets/LogoMio.svg" ] && [ -d "$dir/apps/desktop" ]; then printf '%s\n' "$dir"; return 0; fi
    dir="$(dirname "$dir")"
  done
  return 1
}
ROOT=""
for c in "$EXPLICIT_ROOT" "$SCRIPT_DIR" "$(pwd -P)"; do
  [ -n "$c" ] || continue
  if ROOT="$(find_root "$c")"; then break; fi
  ROOT=""
done
[ -n "$ROOT" ] || fail "repository root not found (no ancestor contains assets/LogoMio.svg)" 2

DESKTOP="$ROOT/apps/desktop"
TAURI_DIR="$DESKTOP/src-tauri"
CONF="$TAURI_DIR/tauri.conf.json"
[ -f "$CONF" ] || fail "Tauri config missing: $CONF (apps/desktop shell not ready)" 2

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  MINGW*|MSYS*|CYGWIN*) OS=windows ;;
  *) fail "unsupported OS: $(uname -s)" 2 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=arm64 ;;
  *) fail "unsupported arch: $(uname -m)" 2 ;;
esac
[ "$OS" = "windows" ] && ARCH=x64

LOG_DIR="$ROOT/.local/logs"; mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/package-$(date '+%Y%m%d-%H%M%S').log"
exec > >(tee -a "$LOG_FILE") 2>&1

command -v node >/dev/null 2>&1 || fail "Node.js is required to build the frontend" 2
json_get() { node -e 'const fs=require("fs");try{const c=JSON.parse(fs.readFileSync(process.argv[1],"utf8"));const v=c[process.argv[2]];process.stdout.write(v===undefined||v===null?"":String(v))}catch(e){process.exit(1)}' "$1" "$2"; }
PRODUCT="$(json_get "$CONF" productName)"; [ -n "$PRODUCT" ] || PRODUCT="NL VO Shield"
VERSION="$(json_get "$CONF" version)"
[ -n "$VERSION" ] || VERSION="$(json_get "$DESKTOP/package.json" version)"
[ -n "$VERSION" ] || fail "cannot determine the product version from tauri.conf.json/package.json" 2

if [ -z "$BUNDLES" ]; then
  case "$OS" in
    macos) BUNDLES="dmg,app" ;;
    linux) BUNDLES="deb,appimage" ;;
    windows) BUNDLES="nsis,msi" ;;
  esac
fi

log INFO "NL VO Shield - package"
log INFO "repo root: $ROOT"
log INFO "product: $PRODUCT | version: $VERSION | ${OS}-${ARCH}"
log INFO "bundle targets: $BUNDLES"

# ---------------------------------------------------------------- leak guard
if [ "$SKIP_LEAK_GUARD" -eq 0 ]; then
  log STEP 'step 1/8 - leak guard (repository)'
  node "$ROOT/tooling/leak-guard.mjs" || fail "leak guard found violations - packaging aborted (fix or move those files)" 3
  log OK 'leak guard: clean'
else
  log WARN 'step 1/8 - leak guard SKIPPED by request (recorded in BUILD-INFO.json)'
fi

# ------------------------------------------------------------- frontend build
log STEP 'step 2/8 - frontend dependencies and build'
cd "$DESKTOP"
if [ ! -d node_modules ]; then
  [ "$SKIP_INSTALL" -eq 1 ] && fail "apps/desktop/node_modules missing and --skip-install was specified" 2
  if [ -f pnpm-lock.yaml ]; then log INFO 'pnpm install --frozen-lockfile'; pnpm install --frozen-lockfile || fail 'dependency installation failed' 1
  elif [ -f package-lock.json ]; then log INFO 'npm ci'; npm ci || fail 'dependency installation failed' 1
  else log INFO 'npm install (no lockfile)'; npm install || fail 'dependency installation failed' 1
  fi
else
  log INFO 'node_modules already present - skipping dependency installation'
fi
log INFO 'npm run build'
npm run build || fail 'frontend build failed' 1
log OK 'frontend build complete'

TAURI_CLI="$DESKTOP/node_modules/@tauri-apps/cli/tauri.js"
[ -f "$TAURI_CLI" ] || fail "Tauri CLI missing after install: $TAURI_CLI" 2

# ------------------------------------------------------------------- sidecar
if [ "$SKIP_SIDECAR" -eq 0 ]; then
  log STEP 'step 3/8 - engine sidecar'
  SIDECAR_ARGS=()
  [ "$WITH_AUDIOSEAL" -eq 1 ] && SIDECAR_ARGS+=(--with-audioseal)
  bash "$ROOT/tooling/build-engine-sidecar.sh" "${SIDECAR_ARGS[@]}" || fail 'engine sidecar build failed' 1
  log OK 'engine sidecar ready'
else
  log WARN 'step 3/8 - sidecar build skipped by request'
fi
[ -d "$TAURI_DIR/engine" ] || fail "engine payload missing: $TAURI_DIR/engine (run tooling/build-engine-sidecar.sh)" 2

# --------------------------------------------------------------- tauri build
log STEP 'step 4/8 - tauri build bundle'
SIGNED=0
if [ -n "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
  SIGNED=1; log INFO 'TAURI_SIGNING_PRIVATE_KEY present: updater artifacts will be signed by the bundler'
else
  log WARN 'TAURI_SIGNING_PRIVATE_KEY absent: artifacts will be UNSIGNED and auto-update stays disabled'
fi
rm -rf "$TAURI_DIR/target/release/bundle"
set +e
node "$TAURI_CLI" build --bundles "$BUNDLES"
TAURI_EXIT=$?
set -e
if [ "$TAURI_EXIT" -ne 0 ]; then
  log WARN "tauri build failed (exit $TAURI_EXIT); retrying with updater artifacts disabled"
  OVERRIDE="$ROOT/.local/cache/tauri-noupdater.json"
  mkdir -p "$(dirname "$OVERRIDE")"
  printf '{ "bundle": { "createUpdaterArtifacts": false } }\n' > "$OVERRIDE"
  node "$TAURI_CLI" build --bundles "$BUNDLES" --config "$OVERRIDE" || fail "tauri build failed" 1
fi
[ -d "$TAURI_DIR/target/release/bundle" ] || fail "tauri build produced no bundle directory" 1

# -------------------------------------------------------------- collect dist
log STEP 'step 5/8 - collecting artifacts into dist'
DIST_ROOT="$ROOT/dist"; DIST="$DIST_ROOT/$VERSION"
rm -rf "$DIST"; mkdir -p "$DIST"

stable_name() { # kind ext
  local kind="$1" ext="$2" base
  case "$kind" in
    nsis) base="${PRODUCT}_${VERSION}_${OS}-${ARCH}-setup" ;;
    msi) base="${PRODUCT}_${VERSION}_${OS}-${ARCH}" ;;
    dmg) base="${PRODUCT}_${VERSION}_${OS}-${ARCH}" ;;
    app) base="${PRODUCT}_${VERSION}_${OS}-${ARCH}.app" ;;
    deb|appimage|rpm) base="${PRODUCT}_${VERSION}_${OS}-${ARCH}" ;;
    *) base="${PRODUCT}_${VERSION}_${kind}" ;;
  esac
  base="$(printf '%s' "$base" | tr -cd 'A-Za-z0-9._-' | sed 's/--*/-/g')"
  printf '%s%s' "$base" "$ext"
}

ARTIFACTS=()
collect_kind() { # kind dir ext
  local kind="$1" dir="$2" ext="$3" f dest
  [ -d "$dir" ] || { log WARN "bundle directory missing: $dir"; return 0; }
  while IFS= read -r -d '' f; do
    dest="$DIST/$(stable_name "$kind" "$ext")"
    cp "$f" "$dest"
    [ -f "$f.sig" ] && cp "$f.sig" "$dest.sig" || true
    ARTIFACTS+=("$(basename "$dest")")
    log OK "artifact: $(basename "$dest") ($(du -h "$dest" | awk '{print $1}'))"
  done < <(find "$dir" -maxdepth 1 -type f -name "*${ext}" -print0)
}
B="$TAURI_DIR/target/release/bundle"
collect_kind nsis "$B/nsis" ".exe"
collect_kind msi "$B/msi" ".msi"
collect_kind dmg "$B/dmg" ".dmg"
collect_kind deb "$B/deb" ".deb"
collect_kind rpm "$B/rpm" ".rpm"
collect_kind appimage "$B/appimage" ".AppImage"
if [ -d "$B/macos" ]; then
  while IFS= read -r -d '' appdir; do
    name="$(stable_name app .tar.gz)"
    tar -czf "$DIST/$name" -C "$(dirname "$appdir")" "$(basename "$appdir")"
    ARTIFACTS+=("$name")
    log OK "artifact: $name ($(du -h "$DIST/$name" | awk '{print $1}'))"
  done < <(find "$B/macos" -maxdepth 1 -type d -name '*.app' -print0)
fi
[ "${#ARTIFACTS[@]}" -gt 0 ] || fail 'no installer artifact was produced by tauri build' 1

# -------------------------------------------------------- checksums + info
log STEP 'step 6/8 - checksums and BUILD-INFO.json'
(
  cd "$DIST"
  for f in "${ARTIFACTS[@]}"; do
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$f"; else shasum -a 256 "$f"; fi
  done
) > "$DIST/SHA256SUMS"

LEAK_GUARD_STATE=passed; [ "$SKIP_LEAK_GUARD" -eq 1 ] && LEAK_GUARD_STATE=skipped
AUTO_UPDATE="disabled-no-signing-key"; [ "$SIGNED" -eq 1 ] && AUTO_UPDATE="enabled-if-updater-pubkey-configured"
AUDIO_JSON=false; [ "$WITH_AUDIOSEAL" -eq 1 ] && AUDIO_JSON=true
SIGNED_JSON=false; [ "$SIGNED" -eq 1 ] && SIGNED_JSON=true
TOTAL_BYTES=0
for f in "${ARTIFACTS[@]}"; do
  if stat -c %s "$DIST/$f" >/dev/null 2>&1; then s=$(stat -c %s "$DIST/$f"); else s=$(stat -f %z "$DIST/$f"); fi
  TOTAL_BYTES=$((TOTAL_BYTES + s))
done
{
  printf '{\n'
  printf '  "schema": 1,\n'
  printf '  "product": "%s",\n' "$PRODUCT"
  printf '  "version": "%s",\n' "$VERSION"
  printf '  "os": "%s",\n' "$OS"
  printf '  "arch": "%s",\n' "$ARCH"
  printf '  "generated_at": "%s",\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '  "signed": %s,\n' "$SIGNED_JSON"
  printf '  "auto_update": "%s",\n' "$AUTO_UPDATE"
  printf '  "leak_guard": "%s",\n' "$LEAK_GUARD_STATE"
  printf '  "bundle_targets": "%s",\n' "$BUNDLES"
  printf '  "with_audio_seal": %s,\n' "$AUDIO_JSON"
  printf '  "models_bundled": false,\n'
  printf '  "models_location": "<app_data>/models (Windows: %%APPDATA%%\\com.nicotina4ever.nlvoshield\\models; portable: <exe folder>/NL VO Shield Data/models)",\n'
  printf '  "artifacts_bytes": %s,\n' "$TOTAL_BYTES"
  printf '  "artifacts": [\n'
  i=0
  for f in "${ARTIFACTS[@]}"; do
    if command -v sha256sum >/dev/null 2>&1; then h=$(sha256sum "$DIST/$f" | awk '{print $1}'); else h=$(shasum -a 256 "$DIST/$f" | awk '{print $1}'); fi
    if stat -c %s "$DIST/$f" >/dev/null 2>&1; then s=$(stat -c %s "$DIST/$f"); else s=$(stat -f %z "$DIST/$f"); fi
    i=$((i + 1))
    [ "$i" -lt "${#ARTIFACTS[@]}" ] && sep=',' || sep=''
    printf '    { "file": "%s", "bytes": %s, "sha256": "%s" }%s\n' "$f" "$s" "$h" "$sep"
  done
  printf '  ]\n}\n'
} > "$DIST/BUILD-INFO.json"

# ------------------------------------------------------- artifact content scan
log STEP 'step 7/8 - artifact content scan'
node "$ROOT/tooling/leak-guard.mjs" --scan "$DIST" || fail 'artifact content scan failed: private paths/lab/runtime traces inside the bundle' 3
log OK 'artifact content scan: clean (no lab/private paths, no model/audio/key payloads)'

log STEP 'step 8/8 - summary'
log OK "dist: $DIST ($TOTAL_BYTES bytes in ${#ARTIFACTS[@]} artifacts)"
for f in "${ARTIFACTS[@]}"; do
  if stat -c %s "$DIST/$f" >/dev/null 2>&1; then s=$(stat -c %s "$DIST/$f"); else s=$(stat -f %z "$DIST/$f"); fi
  log INFO "  $f - $s bytes"
done
log INFO "SHA256SUMS: $DIST/SHA256SUMS"
log INFO "BUILD-INFO.json: $DIST/BUILD-INFO.json"
log INFO "log: $LOG_FILE"
[ "$SIGNED" -eq 0 ] && log WARN 'UNSIGNED build: OS trust warnings are expected; auto-update is disabled until a signing key is configured.'
exit 0
