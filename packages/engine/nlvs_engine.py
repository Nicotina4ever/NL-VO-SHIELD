#!/usr/bin/env python3
"""Robust CLI launcher for the NL VO Shield engine (isolated Python safe).

The project Python is compiled with ``isolated=1`` / ``ignore_environment=1``,
so neither the current directory nor PYTHONPATH are used for imports. This
launcher inserts its own directory into ``sys.path`` before importing the
package, therefore it works from any working directory.

Usage:
    python nlvs_engine.py doctor --json
    python nlvs_engine.py protect <input> --output-dir <dir>
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from nl_vo_shield.cli import main
except Exception as exc:  # pragma: no cover - startup failure must be visible
    sys.stderr.write(f"NL VO Shield engine: cannot import nl_vo_shield: {type(exc).__name__}: {exc}\n")
    raise SystemExit(1)

if __name__ == "__main__":
    raise SystemExit(main())
