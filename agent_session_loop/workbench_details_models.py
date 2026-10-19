# ruff: noqa: F401
"""The Mac provider behind the portable detail reads.

``asl_workbench.details`` says what a detail read is allowed to name and how
wide a window it may take; this module is the half that opens the files. It is
to :mod:`asl_workbench.details` what :mod:`.workbench_catalog` is to
``asl_workbench.catalog``, and the same two properties hold:

* **Nothing here reclaims.** Every plan is classified through
  :func:`.workbench_catalog.build_summary`, every lock is read with
  ``reclaim=False``, and the recovery card and question context are built the
  same way. Before this module, ``GET /api/state``, ``GET /api/verification``,
  ``GET /api/recovery`` and ``GET /api/questions`` still reclaimed a stale plan
  lock as a side effect of describing it -- so opening a plan's detail panel
  repaired the corpus, at whatever rate a dashboard happened to refresh.
  ``observational-reads`` in ``asl_workbench.changes`` records the target.

* **Nothing here reads outside the selected plan.** A read names an artifact
  and :class:`asl_workbench.details.DetailScope` resolves it against what that
  plan and run published. The compatibility form that still accepts a path
  goes through the scope's containment check, so a path is honoured only when
  the plan already owns it.

The payload each ``read_*`` returns is the body of one existing Workbench
detail response, field for field. Keeping them identical is the point: the
routes become request parsing plus a call, the browser sees what it saw
before, and a remote connector serving the same payloads cannot answer with
less than the local one does.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asl_workbench import catalog as portable_catalog
from asl_workbench import details
from asl_workbench.catalog import PlanDetail
from asl_workbench.details import DetailError, DetailScope, Window

from . import (
    artifacts,
    intervention,
    plans_errors,
    plans_files,
    plans_models,
    plans_progress,
    plans_snapshot,
    qna,
    state,
    state_locks,
    state_payloads,
    tail,
    workbench_catalog,
    workbench_recovery_build,
    workbench_registry_models,
    workbench_summary_helpers,
    workbench_summary_model,
    workbench_workspace,
)
from . import events as events_module

_JSONDict = dict[str, Any]

#: The artifact table, in the order the artifact listing publishes it. Two of
#: the ten are not summary fields: ``final_review_artifact_path`` is derived
#: from the plan directory, and every other entry is whatever the plan's own
#: classification resolved.
ARTIFACT_NAMES: tuple[str, ...] = details.ARTIFACT_NAMES


# ---------------------------------------------------------------------------
# Opening a plan


@dataclass(frozen=True)
class PlanReader:
    """One plan opened for reading, classified without repairing it.

    :attr:`scope` is the portable half -- the artifact table and containment
    rule a read consults -- and :attr:`summary` is the Mac-side classification
    it was built from, kept so a read that needs a field the scope does not
    carry (``harness``, ``review_resume``, ``cwd``) does not classify the plan
    a second time.
    """

    plan_dir: Path
    plan_state_dir: Path
    summary: workbench_summary_model.WorkbenchSummary
    scope: DetailScope
    state_dir: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.plan_state_dir / events_module.STATE_FILE_NAME

    @property
    def events_path(self) -> Path:
        return self.plan_state_dir / events_module.EVENTS_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self.plan_state_dir / state.LOCK_FILE_NAME

    def detail(self) -> PlanDetail:
        """The private half of this plan, for an online search or a fetch."""
        return workbench_catalog.record_for_summary(
            self.summary,
            pinned="registry" in self.summary.sources,
        ).detail

    def read(self, kind: str, payload: Mapping[str, Any]) -> details.DetailRead:
        """Wrap one payload in the envelope both providers answer with."""
        return details.build_read(kind, self.scope, payload)
