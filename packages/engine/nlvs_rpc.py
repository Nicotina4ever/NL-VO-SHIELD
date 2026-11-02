#!/usr/bin/env python3
"""Robust JSON-RPC launcher for the NL VO Shield engine (isolated Python safe).

The project Python ignores the current directory and PYTHONPATH; this launcher
adds its own directory to ``sys.path`` first, so it works from any cwd:

    python nlvs_rpc.py --data-dir <dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from nl_vo_shield.rpc import main
except Exception as exc:  # pragma: no cover - startup failure must be visible
    sys.stderr.write(f"NL VO Shield engine: cannot import nl_vo_shield.rpc: {type(exc).__name__}: {exc}\n")
    raise SystemExit(1)

if __name__ == "__main__":
    raise SystemExit(main())
