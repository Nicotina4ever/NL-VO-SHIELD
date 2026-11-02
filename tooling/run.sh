#!/usr/bin/env bash
# NL VO Shield - run the Python engine from a project-local runtime.
#
# Never uses system python: resolves NLVS_ENGINE_PYTHON, then the portable runtime
# in .local/runtime/<target>, then fails with instructions.
#
# Usage:
#   ./tooling/run.sh                  # engine doctor
#   ./tooling/run.sh --version
#   ./tooling/run.sh protect --input <file> --output-dir <dir> [...]
#   ./tooling/run.sh --rpc --data-dir <dir>   # JSON-RPC NDJSON sidecar (dev)
#
# Environment: NLVS_PROJECT_ROOT and NLVS_ENGINE_PYTHON are honoured.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

find_root() {
  local dir="$1"
  while [ -n "$dir" ] && [ "$dir" != "/" ]; do
    if [ -f "$dir/assets/LogoMio.svg" ] && [ -d "$dir/packages" ]; then printf '%s\n' "$dir"; return 0; fi
    dir="$(dirname "$dir")"
  done
  return 1
}

ROOT=""
for candidate in "${NLVS_PROJECT_ROOT:-}" "$SCRIPT_DIR" "$(pwd -P)"; do
  [ -n "$candidate" ] || continue
  if ROOT="$(find_root "$candidate")"; then break; fi
  ROOT=""
done
[ -n "$ROOT" ] || { echo "[run] ERROR: repository root not found (no ancestor contains assets/LogoMio.svg)" >&2; exit 2; }

OS_RAW="$(uname -s)"; ARCH_RAW="$(uname -m)"
case "$OS_RAW" in Darwin) OS=macos ;; Linux) OS=linux ;; *) echo "[run] ERROR: unsupported OS: $OS_RAW" >&2; exit 2 ;; esac
case "$ARCH_RAW" in x86_64|amd64) ARCH=x86_64 ;; arm64|aarch64) ARCH=aarch64 ;; *) echo "[run] ERROR: unsupported arch: $ARCH_RAW" >&2; exit 2 ;; esac
TARGET="${OS}-${ARCH}"

PYTHON=""
if [ -n "${NLVS_ENGINE_PYTHON:-}" ] && [ -x "${NLVS_ENGINE_PYTHON}" ]; then
  PYTHON="${NLVS_ENGINE_PYTHON}"
else
  for candidate in "$ROOT/.local/runtime/${TARGET}/bin/python3" "$ROOT/.local/runtime/${TARGET}/bin/python"; do
    if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
  done
fi
if [ -z "$PYTHON" ]; then
  echo "[run] ERROR: no project Python. Run ./tooling/bootstrap.sh first (or set NLVS_ENGINE_PYTHON)." >&2
  exit 2
fi

ENGINE_PATH="$ROOT/packages/engine"
if [ ! -d "$ENGINE_PATH/nl_vo_shield" ]; then
  echo "[run] ERROR: packages/engine/nl_vo_shield missing - the engine is not installed." >&2
  exit 2
fi

# Process-local environment only; the caller's shell is not modified.
export PYTHONNOUSERSITE=1
export PYTHONUTF8=1
export NLVS_PROJECT_ROOT="$ROOT"
export NLVS_ENGINE_PYTHON="$PYTHON"
mkdir -p "$ROOT/.local/tmp"
export TMPDIR="$ROOT/.local/tmp"

# The Windows project interpreter is an embedded CPython with a ._pth file, which
# ignores PYTHONPATH: insert the engine path explicitly and run the module via runpy.
# This works for both embedded and regular interpreters.
run_module() {
  local module="$1"; shift
  exec "$PYTHON" -c '
import runpy, sys
engine_path, module = sys.argv[1], sys.argv[2]
sys.path.insert(0, engine_path)
sys.argv = [module] + sys.argv[3:]
runpy.run_module(module, run_name="__main__")
' "$ENGINE_PATH" "$module" "$@"
}

RPC_MODE=0
ARGS=()
for arg in "$@"; do
  if [ "$arg" = "--rpc" ] && [ "${#ARGS[@]}" -eq 0 ] && [ "$RPC_MODE" -eq 0 ]; then RPC_MODE=1; continue; fi
  ARGS+=("$arg")
done

if [ "$RPC_MODE" -eq 1 ]; then
  run_module nl_vo_shield.rpc "${ARGS[@]}"
fi
if [ "${#ARGS[@]}" -eq 0 ]; then
  run_module nl_vo_shield.cli doctor
fi
run_module nl_vo_shield.cli "${ARGS[@]}"
