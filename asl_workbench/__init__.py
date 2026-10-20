"""The portable half of the unified ASL Workbench.

This package is the shared application: the capability/ownership matrix, the
transport and compatibility contracts, and -- as later sessions land it -- the
HTTP application, browser assets, and Pi persistence. Two properties make it
"portable", and both are enforced rather than described:

* **It runs on the Pi.** The deployed host is on Python 3.13 while ``scripts``
  tooling targets 3.14, so :data:`asl_workbench.compatibility.MINIMUM_PYTHON_VERSION`
  is the floor and ``asl_workbench`` is pinned to ``py313`` in Ruff's
  ``per-file-target-version`` table. It also depends on nothing but the
  standard library, because the Pi installs no wheels.
* **It does not import ASL's worker runtime.** ``agent_session_loop`` stays the
  authority on discovery, intervention, launch, artifacts, and archives, and
  the Mac-specific providers that reach it live there. A portable module that
  imported it would make the Pi half unloadable and would reopen the
  duplication this program exists to close.

:func:`asl_workbench.compatibility.check_boundary` is the check for both, and
``tests/test_workbench_foundations.py`` runs it plus a subprocess load that
proves the package imports with ``agent_session_loop`` blocked outright.

The matrix in :mod:`asl_workbench.capabilities` is the other half of this
session's deliverable: every Workbench route, CLI entry point, browser
workflow, Companion capability, integration, and persistence boundary, each
naming its sole behavior owner, its compatibility adapters, and the regression
coverage that holds it. ``python3 -m asl_workbench`` renders it.

The public surface is re-exported from the ``exports_*`` modules, grouped by
area; each declares the names it owns and this package merges them.
"""

from __future__ import annotations

from . import exports_connector, exports_foundations, exports_service
from .exports_connector import *  # noqa: F403
from .exports_foundations import *  # noqa: F403
from .exports_service import *  # noqa: F403

__all__ = sorted(
    [
        *exports_foundations.__all__,
        *exports_connector.__all__,
        *exports_service.__all__,
    ]
)
