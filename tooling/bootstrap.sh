#!/usr/bin/env bash
# NL VO Shield - development bootstrap for macOS / Linux (idempotent).
#
# Isolated, repository-local runtime only:
#   - prefers a portable CPython in .local/runtime/<target> (python-build-standalone);
#   - a venv is created only with --venv, only inside .local/envs and only from the
#     portable interpreter (never from system python);
#   - never uses /usr/bin/python*, never changes global PATH or shell profiles;
#   - no silent downloads: portable CPython is fetched only with --allow-download,
#     with an explicit banner and a pinned SHA-256 check.
#
# Usage:
#   ./tooling/bootstrap.sh                  # install/update (needs existing portable python)
#   ./tooling/bootstrap.sh --allow-download # DEV: also provision portable CPython if missing
#   ./tooling/bootstrap.sh --check          # verification only, writes nothing
#   ./tooling/bootstrap.sh --with-optional  # also install requirements-optional.txt (torch/AudioSeal)
#   ./tooling/bootstrap.sh --venv           # create .local/envs/engine from the portable python
#
# Exit codes: 0 ready; 1 check/dependency failure; 2 Python not found;
#             3 pip could not be repaired; 4 package installation failed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

CHECK=0
WITH_OPTIONAL=0
ALLOW_DOWNLOAD="${NLVS_ALLOW_DOWNLOAD:-0}"
USE_VENV=0
EXPLICIT_ROOT=""

usage() {
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --with-optional) WITH_OPTIONAL=1 ;;
    --allow-download) ALLOW_DOWNLOAD=1 ;;
    --venv) USE_VENV=1 ;;
    --root) shift; EXPLICIT_ROOT="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] unknown argument: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

log() {
  local level="$1"; shift
  printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$*"
}
die() {
  local code="$1"; shift
  log ERROR "$*"
  exit "$code"
}

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/assets/LogoMio.svg" ] && [ -d "$dir/packages" ]; then
      printf '%s\n' "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

ROOT=""
for candidate in "$EXPLICIT_ROOT" "$SCRIPT_DIR" "$(pwd -P)"; do
  [ -n "$candidate" ] || continue
  if ROOT="$(find_root "$candidate")"; then break; fi
  ROOT=""
done
[ -n "$ROOT" ] || { log ERROR "repository root not found (no ancestor contains assets/LogoMio.svg). Pass --root <path>."; exit 2; }

# ------------------------------------------------------------------ platform
OS_RAW="$(uname -s)"
ARCH_RAW="$(uname -m)"
case "$OS_RAW" in
  Darwin) OS=macos ;;
  Linux)  OS=linux ;;
  *)      log ERROR "unsupported OS: $OS_RAW (macOS and Linux are supported by this script)"; exit 2 ;;
esac
case "$ARCH_RAW" in
  x86_64|amd64) ARCH=x86_64 ;;
  arm64|aarch64) ARCH=aarch64 ;;
  *) log ERROR "unsupported architecture: $ARCH_RAW"; exit 2 ;;
esac
TARGET="${OS}-${ARCH}"

# Pinned python-build-standalone release (DEV fallback only, checksum verified).
PBS_TAG="20261026"
PBS_VERSION="3.12.16"
PBS_BASE_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}"
case "$TARGET" in
  linux-x86_64)   PBS_FILE="cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz";  PBS_SHA256="ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20" ;;
  linux-aarch64)  PBS_FILE="cpython-3.12.16+20261026-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz"; PBS_SHA256="bb4b6392670c5d3f93876996fcaa19858a4a413a0c7720144c18b0d6f1f0db2d" ;;
  macos-x86_64)   PBS_FILE="cpython-3.12.16+20261026-x86_64-apple-darwin-install_only_stripped.tar.gz";       PBS_SHA256="37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734" ;;
  macos-aarch64)  PBS_FILE="cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz";      PBS_SHA256="353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e" ;;
  *) log ERROR "no pinned portable Python for $TARGET"; exit 2 ;;
esac

# ------------------------------------------------------------------- logging
if [ "$CHECK" -eq 0 ]; then
  mkdir -p "$ROOT/.local/logs" "$ROOT/.local/cache" "$ROOT/.local/runtime" \
           "$ROOT/.local/envs" "$ROOT/.local/downloads" "$ROOT/.local/tmp"
  LOG_FILE="$ROOT/.local/logs/bootstrap-$(date '+%Y%m%d-%H%M%S').log"
  # Mirror everything to the log; keep the real exit status via PIPESTATUS-free `tee` on fd 1/2.
  exec > >(tee -a "$LOG_FILE") 2>&1
fi

log INFO "NL VO Shield bootstrap - $([ "$CHECK" -eq 1 ] && echo 'CHECK ONLY' || echo 'install')"
log INFO "repository root: $ROOT"
log INFO "platform: $TARGET (${OS_RAW} ${ARCH_RAW})"
log INFO "python rule: NLVS_ENGINE_PYTHON > .local/runtime/${TARGET} (portable) > explicit DEV download"

# -------------------------------------------------------------------- helpers
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else return 1; fi
}

python_ok() {
  [ -x "$1" ] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

# Refuse anything outside the project (no system Python, no PATH python).
assert_project_python() {
  local py="$1" real
  real="$(cd "$(dirname "$py")" && pwd -P)/$(basename "$py")"
  case "$real" in
    "$ROOT"/.local/*) return 0 ;;
    *) # an explicit NLVS_ENGINE_PYTHON override is allowed, but must be absolute and not a system root
       case "$real" in
         /usr/bin/*|/bin/*|/usr/local/bin/*) die 2 "refusing system Python: $real (set NLVS_ENGINE_PYTHON to a project runtime)";;
         *) return 0 ;;
       esac ;;
  esac
}

download_python() {
  if [ "$ALLOW_DOWNLOAD" != "1" ]; then
    log ERROR "portable Python not found in .local/runtime/${TARGET}."
    log INFO "re-run with --allow-download to fetch $PBS_FILE (DEV only, checksum verified)."
    log INFO "pinned SHA-256: $PBS_SHA256"
    exit 2
  fi
  log WARN "DEV-ONLY: downloading portable CPython (explicit request)"
  log WARN "  source: ${PBS_BASE_URL}/${PBS_FILE}"
  log WARN "  pinned SHA-256: ${PBS_SHA256}"
  mkdir -p "$ROOT/.local/downloads"
  local archive="$ROOT/.local/downloads/$PBS_FILE"
  if [ ! -f "$archive" ]; then
    local partial="$archive.part"
    rm -f "$partial"
    if command -v curl >/dev/null 2>&1; then
      curl --fail --location --silent --show-error --retry 3 --output "$partial" "${PBS_BASE_URL}/${PBS_FILE}" \
        || { rm -f "$partial"; die 2 "download failed: $PBS_BASE_URL/$PBS_FILE"; }
    elif command -v wget >/dev/null 2>&1; then
      wget -q -O "$partial" "${PBS_BASE_URL}/${PBS_FILE}" || { rm -f "$partial"; die 2 "download failed"; }
    else
      die 2 "neither curl nor wget is available"
    fi
    mv "$partial" "$archive"
  fi
  local hash
  hash="$(sha256_of "$archive")" || die 2 "no sha256 tool available to verify the runtime"
  if [ "$hash" != "$PBS_SHA256" ]; then
    rm -f "$archive"
    die 2 "checksum mismatch for $PBS_FILE: got $hash expected $PBS_SHA256 (refusing unverified runtime)"
  fi
  log OK "download verified: $PBS_FILE"
  local dest="$ROOT/.local/runtime/${TARGET}"
  mkdir -p "$dest"
  tar -xzf "$archive" -C "$dest" --strip-components=1 || die 2 "archive extraction failed"
  printf '%s\n%s\n' "$PBS_VERSION" "$PBS_FILE" > "$dest/.pbs-version"
  log OK "portable CPython installed at $dest"
}

modules_check() {
  local py="$1"; shift
  "$py" - "$@" <<'PYCODE'
import importlib, sys
mods = sys.argv[1].split(',')
missing = []
for name in mods:
    if not name:
        continue
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - report any import problem
        missing.append(f'{name} ({type(exc).__name__})')
if missing:
    print('MISSING: ' + ', '.join(missing))
    raise SystemExit(1)
print('ALL_OK')
PYCODE
}

# --------------------------------------------------------------- step: python
log STEP "step 1/6 - resolving the project Python"
PYTHON=""
PYTHON_KIND=""
for candidate in "$ROOT/.local/runtime/${TARGET}/bin/python3" "$ROOT/.local/runtime/${TARGET}/bin/python"; do
  if python_ok "$candidate"; then PYTHON="$candidate"; PYTHON_KIND="portable-runtime"; break; fi
done
if [ -z "$PYTHON" ] && [ -n "${NLVS_ENGINE_PYTHON:-}" ] && python_ok "$NLVS_ENGINE_PYTHON"; then
  PYTHON="$NLVS_ENGINE_PYTHON"; PYTHON_KIND="NLVS_ENGINE_PYTHON override"
fi
if [ -z "$PYTHON" ]; then
  if [ "$CHECK" -eq 1 ]; then
    log ERROR "portable Python missing at $ROOT/.local/runtime/${TARGET} (run without --check --allow-download)"
    exit 2
  fi
  download_python
  if python_ok "$ROOT/.local/runtime/${TARGET}/bin/python3"; then PYTHON="$ROOT/.local/runtime/${TARGET}/bin/python3"; PYTHON_KIND="portable-runtime"; fi
fi
[ -n "$PYTHON" ] || die 2 "portable Python still missing after provisioning"
assert_project_python "$PYTHON"
log OK "python ($PYTHON_KIND): $PYTHON"
"$PYTHON" -c 'import sys, platform; print("       version:", sys.version.split()[0], platform.machine())'

# venv is opt-in and always created by the portable interpreter inside .local/envs.
if [ "$USE_VENV" -eq 1 ] && [ "$CHECK" -eq 0 ]; then
  VENV_DIR="$ROOT/.local/envs/engine"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    log STEP "creating venv from the portable interpreter: $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR" || die 4 "venv creation failed (the base install does not need a venv)"
  fi
  PYTHON="$VENV_DIR/bin/python"
  PYTHON_KIND="venv(.local/envs/engine)"
  log OK "venv interpreter: $PYTHON"
fi

# -------------------------------------------------------------------- step pip
log STEP "step 2/6 - pip availability"
PIP_CHECK=0
"$PYTHON" -m pip --version >/dev/null 2>&1 && PIP_CHECK=1 || true
if [ "$PIP_CHECK" -ne 1 ]; then
  if [ "$CHECK" -eq 1 ]; then log ERROR "pip is broken for $PYTHON"; exit 3; fi
  log WARN "pip is broken; repairing with the portable interpreter's ensurepip"
  "$PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || die 3 "ensurepip failed"
  "$PYTHON" -m pip --version >/dev/null 2>&1 || die 3 "pip still broken after ensurepip"
fi
log OK "pip: $("$PYTHON" -m pip --version 2>/dev/null | head -n 1)"

# -------------------------------------------------------- isolated environment
export NLVS_PROJECT_ROOT="$ROOT"
export NLVS_ENGINE_PYTHON="$PYTHON"
export PIP_CACHE_DIR="$ROOT/.local/cache/pip"
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONNOUSERSITE=1
export PYTHONUTF8=1
export TORCH_HOME="$ROOT/.local/cache/torch"
export HF_HOME="$ROOT/.local/cache/huggingface"
export NUMBA_CACHE_DIR="$ROOT/.local/cache/numba"
export TMPDIR="$ROOT/.local/tmp"
if [ "$CHECK" -eq 0 ]; then mkdir -p "$TMPDIR"; fi

# --------------------------------------------------------------- step: deps
log STEP "step 3/6 - engine dependencies"
BASE_MODULES="numpy,scipy,soundfile,librosa,cryptography,pyloudnorm"
REQUIREMENTS="$ROOT/packages/engine/requirements.txt"
OPTIONAL_REQUIREMENTS="$ROOT/packages/engine/requirements-optional.txt"
BASELINE="$ROOT/tooling/requirements-baseline.txt"

state_file="$ROOT/.local/.bootstrap-state.json"
if [ -f "$REQUIREMENTS" ]; then
  req_hash="$(sha256_of "$REQUIREMENTS" 2>/dev/null || echo '')"
elif [ -f "$BASELINE" ]; then
  req_hash="$(sha256_of "$BASELINE" 2>/dev/null || echo '')"
else
  req_hash=""
fi
fast_path=0
if [ "$CHECK" -eq 0 ] && [ -f "$state_file" ] && [ -n "$req_hash" ] && grep -q "\"requirements_sha256\": \"$req_hash\"" "$state_file" 2>/dev/null \
   && grep -q '"status": "completed"' "$state_file" 2>/dev/null \
   && modules_check "$PYTHON" "$BASE_MODULES" >/dev/null 2>&1; then
  fast_path=1
fi

if [ "$CHECK" -eq 1 ]; then
  if modules_check "$PYTHON" "$BASE_MODULES" >/dev/null 2>&1; then log OK "all base modules already importable"; else
    log ERROR "base modules missing: $(modules_check "$PYTHON" "$BASE_MODULES" 2>&1 || true)"; exit 1
  fi
elif [ "$fast_path" -eq 1 ]; then
  log OK "dependencies up to date (requirements hash unchanged, imports verified)"
else
  if [ -f "$REQUIREMENTS" ]; then
    log STEP "pip install -r packages/engine/requirements.txt"
    "$PYTHON" -m pip install --disable-pip-version-check --no-warn-script-location -r "$REQUIREMENTS" || die 4 "pip install failed"
  elif [ -f "$BASELINE" ]; then
    log WARN "packages/engine/requirements.txt missing (engine not ready) - installing tooling/requirements-baseline.txt"
    "$PYTHON" -m pip install --disable-pip-version-check --no-warn-script-location -r "$BASELINE" || die 4 "baseline pip install failed"
  else
    log WARN "no requirements file available - skipping dependency installation"
  fi
  if [ "$WITH_OPTIONAL" -eq 1 ]; then
    if [ -f "$OPTIONAL_REQUIREMENTS" ]; then
      log STEP "pip install -r packages/engine/requirements-optional.txt (torch/AudioSeal)"
      "$PYTHON" -m pip install --disable-pip-version-check --no-warn-script-location -r "$OPTIONAL_REQUIREMENTS" \
        || log WARN "optional requirements failed - base product unaffected"
    else
      log WARN "requirements-optional.txt not present - optional engines skipped"
    fi
  fi
  if [ "$CHECK" -eq 0 ]; then
    mkdir -p "$ROOT/.local"
    {
      printf '{\n  "schema": 1,\n  "status": "completed",\n  "platform": "%s",\n  "python": "%s",\n  "requirements_sha256": "%s",\n  "finished_at": "%s"\n}\n' \
        "$TARGET" "$PYTHON" "$req_hash" "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    } > "$state_file"
  fi
fi

# ------------------------------------------------------- step: verification
log STEP "step 4/6 - verifying imports"
if modules_check "$PYTHON" "$BASE_MODULES" >/dev/null 2>&1; then
  log OK "base modules: numpy, scipy, soundfile, librosa, cryptography, pyloudnorm"
else
  log ERROR "module verification failed: $(modules_check "$PYTHON" "$BASE_MODULES" 2>&1 || true)"
  [ "$CHECK" -eq 1 ] && exit 1 || exit 4
fi

ENGINE_PATH="$ROOT/packages/engine"
if [ -d "$ENGINE_PATH/nl_vo_shield" ]; then
  if PYTHONPATH="$ENGINE_PATH" "$PYTHON" -c 'import nl_vo_shield; print("NLVS_ENGINE_IMPORT_OK", getattr(nl_vo_shield, "__version__", "?"))'; then
    log OK "engine package importable"
  else
    log WARN "engine package not importable yet"
  fi
else
  log WARN "packages/engine/nl_vo_shield not present yet (engine module not ready)"
fi

if [ "$WITH_OPTIONAL" -eq 1 ] || [ "$CHECK" -eq 1 ]; then
  if modules_check "$PYTHON" "torch,audioseal,PyInstaller" >/dev/null 2>&1; then
    log OK "optional modules: torch, audioseal, PyInstaller"
  else
    log INFO "optional modules not installed (OK for the base product)"
  fi
fi

# ------------------------------------------------------------ step: finish
log STEP "step 5/6 - executable permissions"
if [ "$CHECK" -eq 0 ]; then
  chmod +x "$ROOT/tooling/bootstrap.sh" "$ROOT/tooling/run.sh" 2>/dev/null || true
  for script in "$ROOT"/tooling/*.sh; do [ -f "$script" ] && chmod +x "$script" 2>/dev/null || true; done
  log OK "shell scripts marked executable"
else
  log INFO "skip (check-only)"
fi

log STEP "step 6/6 - summary"
log INFO "project root: $ROOT"
log INFO "python: $PYTHON ($PYTHON_KIND)"
if [ "$CHECK" -eq 1 ]; then
  log OK "CHECK PASS - development environment is ready"
  exit 0
fi
log INFO "log: ${LOG_FILE:-console only}"
log OK "bootstrap complete - re-running this script is safe (idempotent)"
exit 0
