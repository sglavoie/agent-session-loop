"""``python3 -m asl_workbench`` -- render the capability/ownership matrix.

The entry point lives here rather than under ``capabilities`` because
``asl_workbench/__init__.py`` imports that module, and running it as
``python3 -m asl_workbench.capabilities`` would then execute an already
imported module a second time -- which the interpreter warns about and which
would give two copies of the matrix in one process.
"""

from __future__ import annotations

from .capabilities import main

if __name__ == "__main__":
    raise SystemExit(main())
