# ruff: noqa: F401, E402, F403, F405, I001
"""Compatibility facade for the run suite implementation."""

from __future__ import annotations

from .run_suite_part_1 import FAST_MODULES, REPO_ROOT
from .run_suite_part_2 import (
    INTEGRATION_MODULES,
    PORTABLE_MODULES,
    SCRIPT_SUITES,
)
from .run_suite_part_3 import (
    _discovered_package_modules,
    _modules_for,
    _RAN_PATTERN,
    _run_shard,
    _shard_commands,
    main,
)

from . import run_suite_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .run_suite_shared import *
from . import run_suite_part_1 as _run_suite_part_1
from . import run_suite_part_2 as _run_suite_part_2
from . import run_suite_part_3 as _run_suite_part_3

_PARTS = (
    _run_suite_part_1,
    _run_suite_part_2,
    _run_suite_part_3,
)
for _part in _PARTS:
    globals().update(
        {
            name: value
            for name, value in vars(_part).items()
            if not name.startswith("__")
        }
    )
for _part in _PARTS:
    _part.__dict__.update(
        {name: value for name, value in globals().items() if not name.startswith("__")}
    )
del _part


if __name__ == "__main__":
    raise SystemExit(main())
