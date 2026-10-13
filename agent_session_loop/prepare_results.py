"""Exit reasons, stage names, and result records for ``asl prepare``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import repository_intent
from .plans import PlanError
from .prepare_corrections import CorrectionOutcome

PREPARE_EXIT_REASON_OK = "ok"
PREPARE_EXIT_REASON_NEEDS_PLAN_ITERATION = "needs-plan-iteration"
PREPARE_EXIT_REASON_VALIDATION_FAILED = "validation-failed"
PREPARE_EXIT_REASON_BASELINE_UNSATISFIED = "baseline-unsatisfied"
PREPARE_EXIT_REASON_NOT_LOOP_READY = "not-loop-ready"
PREPARE_EXIT_REASON_INPUT_ERROR = "input-error"
PREPARE_EXIT_REASON_CWD_ERROR = "cwd-error"
PREPARE_EXIT_REASON_PLAN_STRUCTURE = "plan-structure"
PREPARE_EXIT_REASON_WORKSPACE_REBIND_FAILED = "workspace-rebind-failed"
PREPARE_EXIT_REASON_REGISTRY_REQUIRED = (
    repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED
)

#: Pipeline stage names, in execution order.
PREPARE_STAGE_STRUCTURE = "structure"
PREPARE_STAGE_AUTHORING = "authoring"
PREPARE_STAGE_VALIDATION = "validation"
PREPARE_STAGE_CORRECTIONS = "corrections"
PREPARE_STAGE_BASELINE = "baseline"
PREPARE_STAGE_PREFLIGHT = "preflight"
PREPARE_STAGE_WORKSPACE = "workspace"
PREPARE_STAGE_METADATA = "metadata"
PREPARE_STAGE_PUBLICATION = "publication"

PREPARE_STEP_OK = "ok"
PREPARE_STEP_FAILED = "failed"
PREPARE_STEP_SKIPPED = "skipped"

#: Baseline statuses that count as reusable recorded evidence. ``skip``,
#: ``timeout``, and ``error`` outcomes never satisfy an expectation and are
#: re-run rather than reused.
_REUSABLE_BASELINE_STATUSES = ("pass", "fail")


class _WorkspaceRebindConcurrencyError(PlanError):
    """A retryable rebind race or lock failure with structured CLI context."""

    def __init__(
        self,
        message: str,
        *,
        old_workspace: dict[str, Any],
        new_workspace: dict[str, Any],
        stage: str,
    ) -> None:
        super().__init__(message)
        self.old_workspace = old_workspace
        self.new_workspace = new_workspace
        self.stage = stage


@dataclass(frozen=True)
class PrepareStep:
    """One pipeline stage outcome in a preparation run."""

    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass(frozen=True)
class PrepareBlocker:
    """A structured reason a preparation could not claim readiness."""

    stage: str
    message: str
    session_id: str | None = None
    category: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "message": self.message,
            "session_id": self.session_id,
            "category": self.category,
        }


@dataclass(frozen=True)
class PrepareResult:
    """Aggregated outcome of one ``asl prepare`` pipeline run."""

    ok: bool
    reason: str
    plan_dir: Path | None
    source_path: str | None
    session_ids: tuple[str, ...]
    steps: tuple[PrepareStep, ...]
    blockers: tuple[PrepareBlocker, ...]
    corrections: tuple[CorrectionOutcome, ...] = ()
    baseline: dict[str, Any] | None = None
    marked_loop_ready: bool = False
    run_command: str | None = None
    resume_command: str | None = None
    workspace_rebind: dict[str, Any] | None = None
    workspace: dict[str, Any] | None = None


#: Callable that turns one validation packet into decoded validator JSON.
