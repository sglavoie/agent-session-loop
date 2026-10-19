# ruff: noqa: F401, F403, F405, I001
"""Compatibility facade and entry point for the staging rehearsal.

The reusable implementation lives under :mod:`asl_workbench` so a deployed
release can carry the same rehearsal code beside the application it exercises.
"""

from __future__ import annotations

import sys

from asl_workbench.staging_rehearsal_cli import main
from asl_workbench.staging_rehearsal_journal import *  # noqa: F401, F403
from asl_workbench.staging_rehearsal_models import *  # noqa: F401, F403
from asl_workbench.staging_rehearsal_native import *  # noqa: F401, F403

if __name__ == "__main__":  # pragma: no cover - the rehearsal's entry point
    sys.exit(main())
