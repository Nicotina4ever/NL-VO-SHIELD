#!/usr/bin/env python3
"""Launch the NL VO Shield engine from the source tree (no install needed).

Works with the project's embedded Python distribution, which ignores the
current directory and PYTHONPATH because of its ``python311._pth`` file.

Examples (relative paths, no absolute paths baked in):

    python run.py doctor
    python run.py protect ..\\..\\..\\input\\test.wav --output-dir ..\\..\\..\\output
    python run.py verify ..\\..\\..\\output\\test_protected.wav
    python run.py rpc --data-dir ..\\..\\..\\output\\data
"""

from __future__ import annotations

import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from nl_vo_shield.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
