"""Finding, report, and repair-hint types shared by the preflight checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import (
    repository_scope,
    workspaces,
)
from .contracts import (
    CompletionContract,
)
from .plans_models import PlanSnapshot, ReadinessMetadata
from .repository_intent import RepositoryIntent
from .verification_models import VerificationContext

PreflightSeverity = Literal["red", "yellow", "green"]


PreflightCategory = Literal[
    "plan-structure",
    "canonical-session-id",
    "manifest-readiness",
    "missing-contract",
    "malformed-contract",
    "ambiguity-sentinel",
    "path-reference",
    "repository-footprint",
    "repository-registry-required",
    "verification-scope",
    "verification-duplicate",
    "command-not-runnable",
    "progress-journal-missing",
    "baseline-missing",
    "baseline-stale",
    "baseline-unexpected",
    "baseline-timeout",
    "baseline-near-timeout",
    "workspace-root",
    "repository-registry",
    "repository-root",
    "repository-origin",
    "repository-overlap",
    "repository-scope",
    "repository-contract",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-lock",
    "env-amx",
    "env-turn-budget",
]


PREFLIGHT_CATEGORIES: tuple[PreflightCategory, ...] = (
    "plan-structure",
    "canonical-session-id",
    "manifest-readiness",
    "missing-contract",
    "malformed-contract",
    "ambiguity-sentinel",
    "path-reference",
    "repository-footprint",
    "repository-registry-required",
    "verification-scope",
    "verification-duplicate",
    "command-not-runnable",
    "progress-journal-missing",
    "baseline-missing",
    "baseline-stale",
    "baseline-unexpected",
    "baseline-timeout",
    "baseline-near-timeout",
    "workspace-root",
    "repository-registry",
    "repository-root",
    "repository-origin",
    "repository-overlap",
    "repository-scope",
    "repository-contract",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-lock",
    "env-amx",
    "env-turn-budget",
)


#: Categories no ``--allow-not-loop-ready`` escape hatch may waive. A plan that
#: reaches outside its bound root has no snapshot covering the other location,
#: so a run there is unguarded rather than merely unreviewed. The same holds for
#: a plan whose declared intent reaches several repositories without a registry:
#: the missing registry is what would have bound those repositories in the first
#: place, so no readiness marking can stand in for it.
NON_BYPASSABLE_PREFLIGHT_CATEGORIES: frozenset[str] = frozenset(
    {"repository-footprint", "repository-registry-required"}
)


CommitMode = Literal["per-session", "end"]


COMMIT_MODES: tuple[CommitMode, ...] = ("per-session", "end")


@dataclass(frozen=True)
class PreflightFinding:
    """Structured outcome for a single preflight observation."""

    severity: PreflightSeverity
    category: PreflightCategory
    message: str
    session_id: str | None = None
    path: str | None = None
    line: int | None = None
    detail: str = ""


FixCategory = Literal[
    "plan-recreate",
    "filename-rename",
    "manifest-update",
    "plan-edit",
    "invocation-fix",
    "env-install",
    "env-state",
    "session-recipe-edit",
    "informational",
]


@dataclass(frozen=True)
class RepairHint:
    """Machine-readable repair guidance for a preflight finding or pause reason.

    ``category`` is a :data:`PreflightCategory` value for preflight-derived
    hints and a stable runtime reason string (``"no-contract"``,
    ``"malformed-contract"``, ``"verify-failed"``) for runtime hints. The
    ``check_id`` is unique per hint and stable across releases so guided
    skills can cache fix decisions. ``question_template`` is rendered
    verbatim by the consuming skill and supports ``str.format`` keys drawn
    from :class:`PreflightFinding` field names (``session_id``, ``path``,
    ``line``, ``detail``, ``message``).
    """

    category: str
    check_id: str
    severity_hint: PreflightSeverity
    message: str
    question_template: str
    fix_category: FixCategory


@dataclass(frozen=True)
class RepositorySummary:
    """Ordered declared and resolved identity for one workspace repository."""

    alias: str
    path: str
    origin: str
    resolved_root: str | None = None


@dataclass(frozen=True)
class PreflightReport:
    """Aggregated preflight result for a plan directory."""

    plan_dir: Path
    findings: tuple[PreflightFinding, ...] = ()
    snapshot: PlanSnapshot | None = None
    readiness: ReadinessMetadata | None = None
    expected_readiness_sha256: str | None = None
    contracts_by_session: dict[str, CompletionContract] = field(default_factory=dict)
    repository_registry: workspaces.RepositoryRegistry | None = None
    verification_context: VerificationContext | None = None
    repositories: tuple[RepositorySummary, ...] = ()
    repository_footprint: repository_scope.RepositoryFootprint | None = None
    repository_intent: RepositoryIntent | None = None

    @property
    def ok(self) -> bool:
        return not any(
            finding.severity in {"red", "yellow"} for finding in self.findings
        )

    @property
    def red_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == "red")

    @property
    def yellow_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "yellow"
        )

    @property
    def green_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "green"
        )

    @property
    def repair_hints(self) -> tuple[RepairHint, ...]:
        """One :class:`RepairHint` per finding, in finding order."""
        # Local: preflight_hint_resolution imports this module for its types.
        from .preflight_hint_resolution import repair_hint_for_finding

        return tuple(repair_hint_for_finding(f) for f in self.findings)


def _finding(
    severity: PreflightSeverity,
    category: PreflightCategory,
    message: str,
    *,
    session_id: str | None = None,
    path: str | None = None,
    line: int | None = None,
    detail: str = "",
) -> PreflightFinding:
    return PreflightFinding(
        severity=severity,
        category=category,
        message=message,
        session_id=session_id,
        path=path,
        line=line,
        detail=detail,
    )
