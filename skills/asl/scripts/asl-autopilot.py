#!/usr/bin/env python3
"""Executable wrapper for the asl autopilot helper module.

Keeps the helper logic in the importable ``asl_autopilot`` module (which tests
load by path) while exposing a stable executable entry point the SKILL workflow
invokes as ``skills/asl/scripts/asl-autopilot.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from asl_autopilot import main  # noqa: E402 - import needs the sys.path insertion above

if __name__ == "__main__":
    raise SystemExit(main())
