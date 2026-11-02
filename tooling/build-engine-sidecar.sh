#!/usr/bin/env bash
# NL VO Shield - build the macOS/Linux engine sidecar payload.
#
# Produces apps/desktop/src-tauri/engine/:
#   runtime/               portable CPython + base engine dependencies
#                          (launched as: runtime/bin/python3 -m nl_vo_shield.rpc)
#   frozen/nlvo-engine/    PyInstaller one-folder frozen engine
#   build-manifest.json    build provenance + health results
#   .nlvs-engine-build     marker: only this directory is ever cleaned
#
# PyInstaller EXCLUDES torch/audioseal by default. `--with-audioseal` includes the
# optional neural watermark stack (much larger package; not the default release).
#
# Only apps/desktop/src-tauri/engine is cleaned, and only when it carries the
# .nlvs-engine-build marker (or --force is passed). Nothing else is removed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
WITH_AUDIOSEAL=0
SKIP_RUNTIME=0
SKIP_PYINSTALLER=0
FORCE=0
KEEP_WORK=0
EXPLICIT_ROOT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --with-audioseal) WITH_AUDIOSEAL=1 ;;
    --skip-runtime) SKIP_RUNTIME=1 ;;
    --skip-pyinstaller) SKIP_PYINSTALLER=1 ;;
    --force) FORCE=1 ;;
    --keep-work) KEEP_WORK=1 ;;
    --root) shift; EXPLICIT_ROOT="${1:-}" ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "[ERROR] unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

log() { printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" "$2"; }
fail() { log ERROR "$1"; exit "${2:-1}"; }

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/assets/LogoMio.svg" ] && [ -d "$dir/packages/engine" ]; then printf '%s\n' "$dir"; return 0; fi
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

case "$(uname -s)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  *) fail "unsupported OS: $(uname -s)" 2 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=aarch64 ;;
  *) fail "unsupported arch: $(uname -m)" 2 ;;
esac
TARGET="${OS}-${ARCH}"

ENGINE_SRC="$ROOT/packages/engine"
ENGINE_PKG="$ENGINE_SRC/nl_vo_shield"
TAURI_DIR="$ROOT/apps/desktop/src-tauri"
ENGINE_DIR="$TAURI_DIR/engine"
MARKER="$ENGINE_DIR/.nlvs-engine-build"
CACHE_DIR="$ROOT/.local/cache/pyinstaller"
LOG_DIR="$ROOT/.local/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/engine-sidecar-$(date '+%Y%m%d-%H%M%S').log"
exec > >(tee -a "$LOG_FILE") 2>&1

log INFO "NL VO Shield - engine sidecar build ($TARGET)"
log INFO "repo root: $ROOT"
log INFO "audio seal (optional torch stack): $WITH_AUDIOSEAL"
[ -d "$ENGINE_PKG" ] || fail "engine package missing: $ENGINE_PKG (engine module not ready)" 2
[ -d "$TAURI_DIR" ] || fail "desktop shell missing: $TAURI_DIR (apps/desktop is not ready)" 2

# --------------------------------------------------------------- python source
PYTHON=""
PYTHON_KIND=""
if [ -n "${NLVS_ENGINE_PYTHON:-}" ] && [ -x "${NLVS_ENGINE_PYTHON}" ]; then
  PYTHON="$NLVS_ENGINE_PYTHON"; PYTHON_KIND="NLVS_ENGINE_PYTHON"
fi
for candidate in "$ROOT/.local/runtime/${TARGET}/bin/python3" "$ROOT/.local/runtime/${TARGET}/bin/python"; do
  [ -n "$PYTHON" ] && break
  if [ -x "$candidate" ]; then PYTHON="$candidate"; PYTHON_KIND="portable-runtime"; fi
done
[ -n "$PYTHON" ] || fail "no project Python found. Run ./tooling/bootstrap.sh first (or set NLVS_ENGINE_PYTHON)." 2
log OK "source python ($PYTHON_KIND): $PYTHON"
log INFO "python version: $("$PYTHON" -c 'import sys; print(sys.version.split()[0])')"

"$PYTHON" -c 'import PyInstaller' >/dev/null 2>&1 || fail "PyInstaller is not installed in the project Python (pip install pyinstaller)" 2
for module in numpy scipy soundfile librosa cryptography pyloudnorm; do
  "$PYTHON" -c "import $module" >/dev/null 2>&1 || fail "missing base dependency '$module' in the project Python (run ./tooling/bootstrap.sh)" 2
done
if [ "$WITH_AUDIOSEAL" -eq 1 ]; then
  for module in torch audioseal; do
    "$PYTHON" -c "import $module" >/dev/null 2>&1 || fail "--with-audioseal requested but '$module' is not installed" 2
  done
fi

# ------------------------------------------------------------------- cleanup
log STEP 'step 1/6 - cleaning the generated engine directory'
[ "$(basename "$ENGINE_DIR")" = "engine" ] || fail "unexpected engine directory: $ENGINE_DIR" 1
[ "$(basename "$(dirname "$ENGINE_DIR")")" = "src-tauri" ] || fail "unexpected engine directory parent: $ENGINE_DIR" 1
if [ -d "$ENGINE_DIR" ] && [ "$(find "$ENGINE_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' | wc -l)" -gt 0 ]; then
  if [ ! -f "$MARKER" ] && [ "$FORCE" -ne 1 ]; then
    fail "refusing to clean '$ENGINE_DIR': no .nlvs-engine-build marker (use --force after inspecting it)" 1
  fi
fi
mkdir -p "$ENGINE_DIR"
find "$ENGINE_DIR" -mindepth 1 -maxdepth 1 ! -name '.gitkeep' -exec rm -rf {} +
printf 'NL VO Shield generated engine payload - safe to delete and rebuild.\nDo not commit.\n' > "$MARKER"
log OK "cleaned generated engine directory (kept .gitkeep)"

BASE_REQ="$ENGINE_SRC/requirements.txt"
OPTIONAL_REQ="$ENGINE_SRC/requirements-optional.txt"
SITE_ROOT=""

# ------------------------------------------------------------------- runtime
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  log STEP 'step 2/6 - bundling portable Python runtime + app into engine/runtime'
  RUNTIME_DIR="$ENGINE_DIR/runtime"
  mkdir -p "$RUNTIME_DIR"
  SRC_PREFIX="$(cd "$(dirname "$PYTHON")/.." && pwd -P)"
  # Standard CPython layout (python-build-standalone / setup-python): exclude heavy trees.
  tar -C "$SRC_PREFIX" \
      --exclude='./lib/python*/site-packages' \
      --exclude='./lib/python*/test' \
      --exclude='./lib/python*/idlelib' \
      --exclude='./lib/python*/tkinter' \
      --exclude='./share' \
      --exclude='./include' \
      --exclude='./__pycache__' \
      -cf - . | tar -C "$RUNTIME_DIR" -xf -
  RUNTIME_PY="$RUNTIME_DIR/bin/python3"
  [ -x "$RUNTIME_PY" ] || RUNTIME_PY="$RUNTIME_DIR/bin/python"
  [ -x "$RUNTIME_PY" ] || fail "runtime copy produced no bin/python3" 1
  SITE_ROOT="$("$RUNTIME_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  [ -n "$SITE_ROOT" ] || fail "could not determine the runtime site-packages path" 1
  mkdir -p "$SITE_ROOT"

  log STEP 'step 3/6 - installing base engine dependencies into the bundled runtime'
  PYPROJECT="$ENGINE_SRC/pyproject.toml"
  PKG_COPIED=0
  if [ -f "$PYPROJECT" ]; then
    SPEC="$ENGINE_SRC"
    [ "$WITH_AUDIOSEAL" -eq 1 ] && SPEC="${ENGINE_SRC}[ai]"
    BUILD_ARGS=(--disable-pip-version-check --no-warn-script-location --target "$SITE_ROOT")
    if "$PYTHON" -c 'import setuptools' >/dev/null 2>&1; then BUILD_ARGS+=(--no-build-isolation); fi
    log INFO "pip install --target $SITE_ROOT $SPEC (engine package + base dependencies)"
    "$PYTHON" -m pip install "${BUILD_ARGS[@]}" "$SPEC" || fail "pip install of the engine package failed" 1
    PKG_COPIED=1
  else
    REQ_ARGS=()
    if [ -f "$BASE_REQ" ]; then REQ_ARGS+=(-r "$BASE_REQ"); else
      log WARN "requirements.txt missing - installing baseline packages"
      REQ_ARGS+=(numpy scipy soundfile librosa cryptography pyloudnorm)
    fi
    if [ "$WITH_AUDIOSEAL" -eq 1 ] && [ -f "$OPTIONAL_REQ" ]; then REQ_ARGS+=(-r "$OPTIONAL_REQ"); fi
    log INFO "pip install --target $SITE_ROOT ${REQ_ARGS[*]}"
    "$PYTHON" -m pip install --disable-pip-version-check --no-warn-script-location --target "$SITE_ROOT" "${REQ_ARGS[@]}" \
      || fail "pip --target failed" 1
  fi

  if [ "$PKG_COPIED" -eq 0 ]; then
    log INFO 'copying nl_vo_shield package into the bundled site-packages'
    cp -a "$ENGINE_PKG" "$SITE_ROOT/nl_vo_shield"
    find "$SITE_ROOT/nl_vo_shield" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
  fi

  log STEP 'step 4/6 - verifying the bundled runtime'
  VERIFY_IMPORTS='sys, numpy, scipy, soundfile, cryptography, nl_vo_shield'
  [ "$WITH_AUDIOSEAL" -eq 1 ] && VERIFY_IMPORTS='sys, numpy, scipy, soundfile, cryptography, nl_vo_shield, torch, audioseal'
  RUNTIME_VER="$(PYTHONNOUSERSITE=1 "$RUNTIME_PY" -c "import $VERIFY_IMPORTS; print('RUNTIME_OK', sys.version.split()[0], getattr(nl_vo_shield, '__version__', '?'))")" \
    || fail "bundled runtime cannot import the engine + base dependencies" 3
  log OK "bundled runtime: $RUNTIME_VER"
  printf 'runtime: %s\n' "$RUNTIME_VER" >> "$MARKER"

  # macOS: ad-hoc sign the bundled native code so a later `codesign --deep` can succeed.
  if [ "$OS" = "macos" ] && command -v codesign >/dev/null 2>&1; then
    log INFO 'macOS: ad-hoc signing the bundled runtime native binaries'
    signed=0
    while IFS= read -r -d '' f; do
      if file "$f" 2>/dev/null | grep -q 'Mach-O'; then
        codesign --force --sign - "$f" >/dev/null 2>&1 && signed=$((signed + 1)) || true
      fi
    done < <(find "$RUNTIME_DIR" -type f -print0)
    log INFO "macOS ad-hoc signings: $signed (notarization requires Apple certificates; unsigned otherwise)"
  fi
fi

# ----------------------------------------------------------------- PyInstaller
FROZEN_EXE=""
if [ "$SKIP_PYINSTALLER" -eq 0 ]; then
  log STEP 'step 5/6 - PyInstaller one-folder frozen engine'
  rm -rf "$CACHE_DIR"; mkdir -p "$CACHE_DIR"
  ENTRY="$ROOT/tooling/sidecar_entry.py"
  [ -f "$ENTRY" ] || fail "missing sidecar entry script: $ENTRY" 2
  FROZEN_DIR="$ENGINE_DIR/frozen"
  EXCLUDES=(torch torchaudio torchvision audioseal transformers matplotlib IPython notebook tkinter)
  [ "$WITH_AUDIOSEAL" -eq 1 ] && EXCLUDES=(matplotlib IPython notebook tkinter)
  PYI_ARGS=(
    -m PyInstaller --noconfirm --clean --onedir --console
    --name nlvo-engine
    --distpath "$FROZEN_DIR"
    --workpath "$CACHE_DIR"
    --specpath "$CACHE_DIR"
    --paths "$ENGINE_SRC"
    --collect-submodules nl_vo_shield
  )
  for module in "${EXCLUDES[@]}"; do PYI_ARGS+=(--exclude-module "$module"); done
  PYI_ARGS+=("$ENTRY")
  log INFO "PyInstaller excludes: ${EXCLUDES[*]}"
  "$PYTHON" "${PYI_ARGS[@]}" || fail "PyInstaller failed" 1
  FROZEN_EXE="$FROZEN_DIR/nlvo-engine/nlvo-engine"
  [ -x "$FROZEN_EXE" ] || fail "PyInstaller produced no executable: $FROZEN_EXE" 1
  log OK "frozen engine: $FROZEN_EXE ($(du -sh "$FROZEN_EXE" 2>/dev/null | awk '{print $1}'))"
  if [ "$KEEP_WORK" -eq 0 ]; then rm -rf "$CACHE_DIR"; fi
fi

# ---------------------------------------------------------------- health checks
log STEP 'step 6/6 - sidecar health checks'
HEALTH_OK=0
HEALTH_TOTAL=0
check_cli() {
  local exe="$1" regargs="$2" desc="$3" out code
  HEALTH_TOTAL=$((HEALTH_TOTAL + 1))
  set +e
  # shellcheck disable=SC2086 - regargs is intentionally word-split
  out="$("$exe" $regargs --version 2>&1)"; code=$?
  set -e
  if [ $code -eq 0 ]; then HEALTH_OK=$((HEALTH_OK + 1)); log OK "$desc: $out"; else log WARN "$desc FAILED: $out"; fi
}
check_rpc() {
  local exe="$1" args="$2" desc="$3" out
  HEALTH_TOTAL=$((HEALTH_TOTAL + 1))
  local data_dir="$ROOT/.local/tmp/rpc-health"
  mkdir -p "$data_dir"
  set +e
  if command -v timeout >/dev/null 2>&1; then
    out="$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"health","params":{}}' \
      | timeout 45 "$exe" $args --data-dir "$data_dir" 2>/dev/null | head -n 1)"
  else
    out="$(printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"health","params":{}}' \
      | "$exe" $args --data-dir "$data_dir" 2>/dev/null | head -n 1)"
  fi
  set -e
  if printf '%s' "$out" | grep -q '"result"'; then HEALTH_OK=$((HEALTH_OK + 1)); log OK "$desc: $out"; else log WARN "$desc FAILED: ${out:-no response}"; fi
}
if [ "$SKIP_RUNTIME" -eq 0 ]; then
  check_cli "$RUNTIME_PY" '-m nl_vo_shield.cli' 'bundled runtime CLI (--version)'
  check_rpc "$RUNTIME_PY" '-m nl_vo_shield.rpc' 'bundled runtime RPC health'
fi
if [ -n "$FROZEN_EXE" ]; then
  check_cli "$FROZEN_EXE" '' 'frozen engine CLI (--version)'
  check_rpc "$FROZEN_EXE" '--rpc' 'frozen engine RPC health'
fi
[ "$HEALTH_TOTAL" -gt 0 ] && [ "$HEALTH_OK" -eq 0 ] && fail "no sidecar entry point passed a health check" 3

# -------------------------------------------------------------------- manifest
# The manifest is shipped inside the installer resources: no absolute build-host
# paths, no health payloads that embed data paths.
REQ_HASH=""
if [ -f "$BASE_REQ" ]; then
  if command -v sha256sum >/dev/null 2>&1; then REQ_HASH="$(sha256sum "$BASE_REQ" | awk '{print $1}')"; else REQ_HASH="$(shasum -a 256 "$BASE_REQ" | awk '{print $1}')"; fi
elif [ -f "$ENGINE_SRC/pyproject.toml" ]; then
  if command -v sha256sum >/dev/null 2>&1; then REQ_HASH="$(sha256sum "$ENGINE_SRC/pyproject.toml" | awk '{print $1}')"; else REQ_HASH="$(shasum -a 256 "$ENGINE_SRC/pyproject.toml" | awk '{print $1}')"; fi
fi
TOTAL_BYTES="$(du -sk "$ENGINE_DIR" | awk '{print $1 * 1024}')"
RUNTIME_JSON='false'; [ "$SKIP_RUNTIME" -eq 0 ] && RUNTIME_JSON='true'
PYI_JSON='false'; [ -n "$FROZEN_EXE" ] && PYI_JSON='true'
AUDIO_JSON='false'; [ "$WITH_AUDIOSEAL" -eq 1 ] && AUDIO_JSON='true'
cat > "$ENGINE_DIR/build-manifest.json" <<JSON
{
  "schema": 1,
  "product": "NL VO Shield",
  "platform": "$TARGET",
  "generated_at": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "with_audio_seal": $AUDIO_JSON,
  "runtime": $RUNTIME_JSON,
  "pyinstaller": $PYI_JSON,
  "python": {"source": "$PYTHON_KIND", "version": "$("$PYTHON" -c 'import sys; print(sys.version.split()[0])')"},
  "requirements_sha256": "$REQ_HASH",
  "health_ok": $HEALTH_OK,
  "health_total": $HEALTH_TOTAL,
  "size_bytes": $TOTAL_BYTES,
  "expected_layout": ["runtime/bin/python3 (-m nl_vo_shield.rpc)", "frozen/nlvo-engine/nlvo-engine"]
}
JSON

log OK "engine payload ready: $ENGINE_DIR ($(du -sh "$ENGINE_DIR" | awk '{print $1}'))"
log INFO "log: $LOG_FILE"
log INFO 'note: macOS signing/notarization requires Apple certificates; the payload is unsigned otherwise.'
exit 0
