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
"""

from __future__ import annotations

from .capabilities import (
    CAPABILITIES,
    Capability,
    MatrixError,
    capability,
    render_matrix,
    surface_keys,
    validate_matrix,
)
from .catalog import (
    CACHE_SAFE_FIELDS,
    PRIVATE_FIELDS,
    CatalogEntry,
    CatalogError,
    CatalogPage,
    CatalogRecord,
    PlanCatalog,
    PlanDetail,
    build_catalog,
    plan_key,
    record_from_summary,
    run_key,
    unclassified_summary_fields,
)
from .changes import INTENTIONAL_CHANGES, IntentionalChange, intentional_change
from .companion_inventory import (
    COMPANION_MODULES,
    COMPANION_ROUTES,
    locate_companion_package,
)
from .compatibility import (
    MINIMUM_PYTHON_VERSION,
    BoundaryReport,
    RuntimeSupport,
    check_boundary,
    check_runtime,
)
from .history import (
    HISTORY_OUTCOMES,
    HISTORY_SUCCESS_OUTCOMES,
    QUESTION_OUTCOMES,
    HistoryBatch,
    HistoryEntry,
    HistoryError,
    HistoryView,
    ImportResult,
    QuestionEntry,
    SourceCheckpoint,
    SourceRead,
    batches,
    build_view,
    classify_outcome,
    combine,
    consume_records,
    project_entry,
    restart_required,
)

__all__ = [
    "CACHE_SAFE_FIELDS",
    "CAPABILITIES",
    "COMPANION_MODULES",
    "COMPANION_ROUTES",
    "HISTORY_OUTCOMES",
    "HISTORY_SUCCESS_OUTCOMES",
    "INTENTIONAL_CHANGES",
    "MINIMUM_PYTHON_VERSION",
    "PRIVATE_FIELDS",
    "QUESTION_OUTCOMES",
    "BoundaryReport",
    "Capability",
    "CatalogEntry",
    "CatalogError",
    "CatalogPage",
    "CatalogRecord",
    "HistoryBatch",
    "HistoryEntry",
    "HistoryError",
    "HistoryView",
    "ImportResult",
    "IntentionalChange",
    "MatrixError",
    "PlanCatalog",
    "PlanDetail",
    "QuestionEntry",
    "RuntimeSupport",
    "SourceCheckpoint",
    "SourceRead",
    "batches",
    "build_catalog",
    "build_view",
    "capability",
    "check_boundary",
    "check_runtime",
    "classify_outcome",
    "combine",
    "consume_records",
    "intentional_change",
    "locate_companion_package",
    "plan_key",
    "project_entry",
    "record_from_summary",
    "render_matrix",
    "restart_required",
    "run_key",
    "surface_keys",
    "unclassified_summary_fields",
    "validate_matrix",
]
