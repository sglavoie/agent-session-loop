"""Preflight checks for split session plan directories.

The preflight engine inspects a plan directory and surfaces structured
findings that downstream tooling (``asl preflight`` and the guided repair
skill) can route to humans or machines. Each finding carries a severity
(``red``, ``yellow``, ``green``), a stable ``category`` string, and the
file/line/session context needed to localize the issue.

Session 5 covers the core checks that run against the durable plan files
alone: plan structure validation, canonical session-id filenames, manifest
readiness metadata (state + stable SHA-256), per-session completion
contracts, ambiguity sentinels in session bodies, and lightweight path
hygiene on contract ``path`` fields. Session 6 adds environment-aware
checks that depend on the runtime working directory, the requested
harness, the active commit mode, and the operational lock state: repo
root, git worktree/dirty status, harness executable availability, ``spec``
availability when commit modes are requested, stale or active locks,
optional Agent Mux ``amx`` hook discovery, and a coarse turn-budget
estimate based on pending session count. ``run_core_preflight`` remains
environment-free; ``run_environment_preflight`` exposes only the
environment findings; ``run_preflight`` aggregates both into a single
report. Session 13 attaches machine-readable repair hints at the report
level — every finding resolves to a stable :class:`RepairHint` via
:func:`repair_hint_for_finding`, and runtime pause reasons emitted by
``asl run``/``asl verify`` (``no-contract``, ``malformed-contract``,
``verify-failed`` plus its per-check ``failure_reason``) resolve via
:func:`runtime_repair_hint`. ``ok`` is reserved as the gate signal: it
is ``True`` only when no red **or** yellow finding was emitted, matching
the discipline ``asl verify`` already uses for its JSON ``ok`` field.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Literal

from . import harnesses, responses, state
from .contracts import (
    COMPLETION_CONTRACT_HEADING,
    CompletionContract,
    FileExistsCheck,
    RegexInFileCheck,
    parse_completion_contract,
)
from .plans import (
    HANDOFF_HEADING,
    PlanError,
    PlanSnapshot,
    ProgressEntry,
    ReadinessMetadata,
    SessionFile,
    canonicalize_path,
    inspect_plan,
    noncanonical_session_file_suggestions,
    parse_readiness_metadata,
    progress_path,
    read_progress_entries,
    stable_readiness_sha256,
    write_readiness_metadata,
)


PreflightSeverity = Literal["red", "yellow", "green"]
PreflightCategory = Literal[
    "plan-structure",
    "canonical-session-id",
    "manifest-readiness",
    "missing-contract",
    "malformed-contract",
    "ambiguity-sentinel",
    "path-reference",
    "progress-journal-missing",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-spec",
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
    "progress-journal-missing",
    "env-repo-root",
    "env-git",
    "env-harness",
    "env-spec",
    "env-lock",
    "env-amx",
    "env-turn-budget",
)

CommitMode = Literal["per-session", "end"]
COMMIT_MODES: tuple[CommitMode, ...] = ("per-session", "end")

TURN_BUDGET_YELLOW_THRESHOLD = 25
TURNS_PER_SESSION_ESTIMATE = 3

WORD_AMBIGUITY_SENTINELS: tuple[str, ...] = (
    "TBD",
    "TODO",
    "FIXME",
    "XXX",
)
LITERAL_AMBIGUITY_SENTINELS: tuple[str, ...] = (
    "<placeholder>",
    "<fill this in>",
    "<fill in>",
)
AMBIGUITY_SENTINELS: tuple[str, ...] = (
    *WORD_AMBIGUITY_SENTINELS,
    *LITERAL_AMBIGUITY_SENTINELS,
)
WORD_AMBIGUITY_SENTINEL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])("
    + "|".join(re.escape(sentinel) for sentinel in WORD_AMBIGUITY_SENTINELS)
    + r")(?![A-Za-z0-9_-])"
)


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


REPAIR_HINTS: dict[PreflightCategory, RepairHint] = {
    "plan-structure": RepairHint(
        category="plan-structure",
        check_id="plan-structure.recreate",
        severity_hint="red",
        message=(
            "Recreate the plan directory with a valid context.md, manifest.md, "
            "and at least one canonical session-N.md file."
        ),
        question_template=(
            "Plan directory at {path} is unreadable or missing required files. "
            "Should I recreate it from the source plan?"
        ),
        fix_category="plan-recreate",
    ),
    "canonical-session-id": RepairHint(
        category="canonical-session-id",
        check_id="canonical-session-id.rename",
        severity_hint="red",
        message=(
            "Rename the non-canonical session file to the canonical session-N.md "
            "form shown in the finding detail."
        ),
        question_template=(
            "Session filename at {path} is non-canonical ({detail}). "
            "Apply the rename?"
        ),
        fix_category="filename-rename",
    ),
    "manifest-readiness": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.repair",
        severity_hint="red",
        message=(
            "Re-run `asl preflight --mark-loop-ready` to refresh the manifest "
            "readiness block, or fix the manifest by hand."
        ),
        question_template=(
            "Manifest readiness metadata needs attention: {message}. "
            "Should I rerun preflight to refresh it?"
        ),
        fix_category="manifest-update",
    ),
    "missing-contract": RepairHint(
        category="missing-contract",
        check_id="missing-contract.add",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file with "
            "`### Done when` bullets and a fenced ```json``` Verify recipe."
        ),
        question_template=(
            "Session {session_id} is missing a Completion Contract. "
            "Should I help you draft one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="malformed-contract.repair",
        severity_hint="red",
        message=(
            "Fix the Completion Contract syntax reported by the parser error "
            "in the finding message."
        ),
        question_template=(
            "Session {session_id} has a malformed Completion Contract: {message}. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "ambiguity-sentinel": RepairHint(
        category="ambiguity-sentinel",
        check_id="ambiguity-sentinel.replace",
        severity_hint="yellow",
        message=(
            "Replace the ambiguity sentinel (TBD/TODO/FIXME/etc.) with "
            "concrete instructions before running the session."
        ),
        question_template=(
            "Session {session_id} contains an ambiguity sentinel at line {line}: "
            "{detail}. Resolve it now?"
        ),
        fix_category="plan-edit",
    ),
    "path-reference": RepairHint(
        category="path-reference",
        check_id="path-reference.normalize",
        severity_hint="yellow",
        message=(
            "Normalize the contract path to a repo-relative path that does not "
            "use absolute paths or '..' traversal."
        ),
        question_template=(
            "Session {session_id} contract path is not repo-relative ({detail}). "
            "Rewrite it?"
        ),
        fix_category="plan-edit",
    ),
    "progress-journal-missing": RepairHint(
        category="progress-journal-missing",
        check_id="progress-journal-missing.backfill",
        severity_hint="yellow",
        message=(
            "Run `asl backfill-progress <plan>` to reconstruct missing "
            "progress.md, or `asl backfill-progress --force <plan>` when "
            "the journal already exists but is stale or malformed."
        ),
        question_template=(
            "Progress journal problem: {message}. "
            "Should I run `asl backfill-progress` to reconstruct it?"
        ),
        fix_category="manifest-update",
    ),
    "env-repo-root": RepairHint(
        category="env-repo-root",
        check_id="env-repo-root.use-cwd",
        severity_hint="red",
        message=(
            "Pass an existing repository directory via `--cwd` when invoking "
            "the command."
        ),
        question_template=(
            "Repo root {path} does not exist or is not a directory. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git": RepairHint(
        category="env-git",
        check_id="env-git.repair",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or resolve the git status "
            "problem shown in the finding detail."
        ),
        question_template=(
            "Git environment problem: {message}. Should I try a fix?"
        ),
        fix_category="env-state",
    ),
    "env-harness": RepairHint(
        category="env-harness",
        check_id="env-harness.install-cli",
        severity_hint="red",
        message=(
            "Install the harness CLI on PATH, or set "
            "`agent_session_loop/harnesses.json` to point at the right executable."
        ),
        question_template=(
            "Harness CLI problem: {message}. "
            "Should I help configure harnesses.json?"
        ),
        fix_category="env-install",
    ),
    "env-spec": RepairHint(
        category="env-spec",
        check_id="env-spec.install",
        severity_hint="red",
        message=(
            "Install `spec` on PATH, or omit `--commit-per-session` / "
            "`--commit-at-end`."
        ),
        question_template=(
            "spec is required for the requested commit mode. "
            "Should I drop the commit-mode flag or help install spec?"
        ),
        fix_category="env-install",
    ),
    "env-lock": RepairHint(
        category="env-lock",
        check_id="env-lock.repair",
        severity_hint="red",
        message=(
            "Run `asl unlock <plan>` to clear a stale lock, or wait for the "
            "active runner to finish."
        ),
        question_template=(
            "Lock contention: {message}. "
            "Should I run `asl unlock` or wait for the runner?"
        ),
        fix_category="env-state",
    ),
    "env-amx": RepairHint(
        category="env-amx",
        check_id="env-amx.optional",
        severity_hint="green",
        message=(
            "Agent Mux integration is optional and never blocks loop-ready. "
            "No fix required."
        ),
        question_template=(
            "Agent Mux hooks are optional ({message}). No action is required."
        ),
        fix_category="informational",
    ),
    "env-turn-budget": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.healthy",
        severity_hint="green",
        message=(
            "Turn budget is within the expected range. No fix required."
        ),
        question_template=(
            "Turn budget is healthy ({message}). No action is required."
        ),
        fix_category="informational",
    ),
}


_CATEGORY_HINT_OVERRIDES: dict[str, RepairHint] = {
    "manifest-readiness.missing": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.missing",
        severity_hint="red",
        message=(
            "Add **Readiness**, **Readiness SHA-256**, and **Readiness checked "
            "at** lines to manifest.md, or run `asl preflight --mark-loop-ready`."
        ),
        question_template=(
            "Manifest is missing readiness metadata. "
            "Should I run `asl preflight --mark-loop-ready` to add it?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.sha-mismatch": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.sha-mismatch",
        severity_hint="red",
        message=(
            "Re-run `asl preflight --mark-loop-ready` after reviewing plan "
            "edits; the recorded SHA-256 no longer matches the plan content."
        ),
        question_template=(
            "Manifest readiness SHA-256 is stale ({detail}). "
            "Should I re-mark the plan loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "manifest-readiness.state-not-loop-ready": RepairHint(
        category="manifest-readiness",
        check_id="manifest-readiness.state-not-loop-ready",
        severity_hint="yellow",
        message=(
            "Advance readiness state from `draft` or `reviewed` to "
            "`loop-ready` with `asl preflight --mark-loop-ready` once all "
            "checks pass."
        ),
        question_template=(
            "Plan readiness state is not loop-ready ({message}). "
            "Should I advance it to loop-ready?"
        ),
        fix_category="manifest-update",
    ),
    "env-git.outside-worktree": RepairHint(
        category="env-git",
        check_id="env-git.outside-worktree",
        severity_hint="red",
        message=(
            "Run from inside a git working tree, or pass `--cwd` pointing "
            "at one."
        ),
        question_template=(
            "Repo root {path} is not inside a git working tree. "
            "Which path should I use as --cwd?"
        ),
        fix_category="invocation-fix",
    ),
    "env-git.dirty-worktree": RepairHint(
        category="env-git",
        check_id="env-git.dirty-worktree",
        severity_hint="yellow",
        message=(
            "Stash or commit dirty files before requesting "
            "`--commit-per-session` or `--commit-at-end`."
        ),
        question_template=(
            "Git worktree is dirty and commit-mode {detail} is requested. "
            "Stash, commit, or drop the commit-mode flag?"
        ),
        fix_category="env-state",
    ),
    "env-lock.stale": RepairHint(
        category="env-lock",
        check_id="env-lock.stale",
        severity_hint="yellow",
        message=(
            "Run `asl unlock <plan>` to clear the stale lock."
        ),
        question_template=(
            "A stale lock is present at {path} ({detail}). "
            "Clear it with `asl unlock`?"
        ),
        fix_category="env-state",
    ),
    "env-lock.active": RepairHint(
        category="env-lock",
        check_id="env-lock.active",
        severity_hint="red",
        message=(
            "Wait for the active runner to finish, or run `asl unlock <plan>` "
            "if you are certain the pid is gone."
        ),
        question_template=(
            "An active lock is held at {path} ({detail}). "
            "Wait for the runner or force-unlock?"
        ),
        fix_category="env-state",
    ),
    "env-turn-budget.split": RepairHint(
        category="env-turn-budget",
        check_id="env-turn-budget.split",
        severity_hint="yellow",
        message=(
            "Split the plan further with `$session-resplit`, or limit the run "
            "with `asl run --max-sessions N`."
        ),
        question_template=(
            "Pending session count exceeds the turn-budget threshold "
            "({message}). Resplit the plan or cap --max-sessions?"
        ),
        fix_category="plan-edit",
    ),
}


RUNTIME_REPAIR_HINTS: dict[str, RepairHint] = {
    "no-contract": RepairHint(
        category="no-contract",
        check_id="runtime.no-contract",
        severity_hint="red",
        message=(
            "Add a `## Completion Contract` section to the session file. The "
            "runner refuses to advance sessions without one."
        ),
        question_template=(
            "Runner paused: session has no Completion Contract. "
            "Should I help you add one?"
        ),
        fix_category="plan-edit",
    ),
    "malformed-contract": RepairHint(
        category="malformed-contract",
        check_id="runtime.malformed-contract",
        severity_hint="red",
        message=(
            "Fix the Completion Contract JSON. Preflight normally catches "
            "this, so check whether the session was edited after the plan was "
            "marked loop-ready."
        ),
        question_template=(
            "Runner paused: malformed Completion Contract. "
            "Should I walk you through the fix?"
        ),
        fix_category="plan-edit",
    ),
    "verify-failed.command-failed": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-failed",
        severity_hint="red",
        message=(
            "Investigate the failing command's exit code; fix the underlying "
            "issue or correct the recipe."
        ),
        question_template=(
            "Verification command exited non-zero. "
            "Show me the stderr and propose a fix?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-timeout": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-timeout",
        severity_hint="red",
        message=(
            "Raise the recipe timeout in the Completion Contract, or fix the "
            "underlying slowness in the command."
        ),
        question_template=(
            "Verification command timed out. "
            "Should I raise the timeout or investigate the slowness?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.command-not-found": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-not-found",
        severity_hint="red",
        message=(
            "Install the missing binary or add it to PATH; alternatively, "
            "change the recipe to invoke an available command."
        ),
        question_template=(
            "Verification command was not found on PATH. "
            "Install the binary or rewrite the recipe?"
        ),
        fix_category="env-install",
    ),
    "verify-failed.command-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.command-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error that prevented command execution "
            "(permissions, missing cwd, etc.) and re-run."
        ),
        question_template=(
            "Verification command could not be executed. "
            "Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-missing": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-missing",
        severity_hint="red",
        message=(
            "Create the expected artifact, or fix the contract `path` field "
            "to point at the right location."
        ),
        question_template=(
            "Expected artifact is missing. "
            "Should I create the file or correct the contract path?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.file-error": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.file-error",
        severity_hint="red",
        message=(
            "Resolve the OS-level error preventing file read/stat "
            "(permissions, encoding, etc.) and re-run."
        ),
        question_template=(
            "Verification could not read the artifact. "
            "Should I diagnose the OS error?"
        ),
        fix_category="session-recipe-edit",
    ),
    "verify-failed.regex-not-matched": RepairHint(
        category="verify-failed",
        check_id="runtime.verify-failed.regex-not-matched",
        severity_hint="red",
        message=(
            "Update the file to contain the expected pattern, or relax the "
            "regex in the recipe."
        ),
        question_template=(
            "Verification regex did not match. "
            "Should I update the file or loosen the pattern?"
        ),
        fix_category="session-recipe-edit",
    ),
}


def _override_check_id_for_finding(finding: PreflightFinding) -> str:
    """Return the override check_id for a finding, or '' if no override applies."""
    if finding.category == "manifest-readiness":
        if "missing readiness" in finding.message:
            return "manifest-readiness.missing"
        if "SHA-256 does not match" in finding.message:
            return "manifest-readiness.sha-mismatch"
        if "readiness state is" in finding.message:
            return "manifest-readiness.state-not-loop-ready"
        return ""
    if finding.category == "env-git":
        if "not inside a git repository" in finding.message:
            return "env-git.outside-worktree"
        if "worktree is dirty" in finding.message:
            return "env-git.dirty-worktree"
        return ""
    if finding.category == "env-lock":
        if "stale lock" in finding.message:
            return "env-lock.stale"
        if "active lock" in finding.message:
            return "env-lock.active"
        return ""
    if finding.category == "env-turn-budget":
        if finding.severity == "yellow":
            return "env-turn-budget.split"
        return ""
    return ""


def repair_hint_for_finding(finding: PreflightFinding) -> RepairHint:
    """Resolve a preflight finding to its canonical repair hint.

    Sub-case overrides (keyed by ``check_id``) win over the category-level
    default in :data:`REPAIR_HINTS`.
    """
    override_id = _override_check_id_for_finding(finding)
    if override_id and override_id in _CATEGORY_HINT_OVERRIDES:
        return _CATEGORY_HINT_OVERRIDES[override_id]
    return REPAIR_HINTS[finding.category]


def runtime_repair_hint(
    reason: str, *, failure_reason: str | None = None
) -> RepairHint | None:
    """Resolve a runner/verify pause reason to its repair hint.

    ``reason`` is the stable ``asl run``/``asl verify`` pause string
    (``"no-contract"``, ``"malformed-contract"``, ``"verify-failed"``).
    For ``verify-failed``, pass the per-check ``failure_reason`` (e.g.,
    ``"command-not-found"``) to resolve a more specific hint. Returns
    ``None`` for unknown reasons so callers can decide whether to fall
    back to a generic message.
    """
    if reason == "verify-failed" and failure_reason is not None:
        return RUNTIME_REPAIR_HINTS.get(f"verify-failed.{failure_reason}")
    return RUNTIME_REPAIR_HINTS.get(reason)


@dataclass(frozen=True)
class PreflightReport:
    """Aggregated preflight result for a plan directory."""

    plan_dir: Path
    findings: tuple[PreflightFinding, ...] = ()
    snapshot: PlanSnapshot | None = None
    readiness: ReadinessMetadata | None = None
    expected_readiness_sha256: str | None = None
    contracts_by_session: dict[str, CompletionContract] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(
            finding.severity in {"red", "yellow"} for finding in self.findings
        )

    @property
    def red_findings(self) -> tuple[PreflightFinding, ...]:
        return tuple(
            finding for finding in self.findings if finding.severity == "red"
        )

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


def _final_handoff_start(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == HANDOFF_HEADING:
            return index
    return None


def _read_session_text(session: SessionFile) -> str | None:
    try:
        return session.path.read_text(encoding="utf-8")
    except OSError:
        return None


def _check_plan_structure(
    canonical_dir: Path,
) -> tuple[tuple[PreflightFinding, ...], PlanSnapshot | None]:
    findings: list[PreflightFinding] = []

    if not canonical_dir.exists():
        findings.append(
            _finding(
                "red",
                "plan-structure",
                f"plan directory does not exist: {canonical_dir}",
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    if not canonical_dir.is_dir():
        findings.append(
            _finding(
                "red",
                "plan-structure",
                f"plan path is not a directory: {canonical_dir}",
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    try:
        snapshot = inspect_plan(canonical_dir)
    except PlanError as error:
        findings.append(
            _finding(
                "red",
                "plan-structure",
                str(error),
                path=str(canonical_dir),
            )
        )
        return tuple(findings), None

    return tuple(findings), snapshot


def _check_canonical_session_ids(canonical_dir: Path) -> tuple[PreflightFinding, ...]:
    try:
        suggestions = noncanonical_session_file_suggestions(canonical_dir)
    except PlanError as error:
        return (
            _finding(
                "red",
                "canonical-session-id",
                f"cannot inspect session filenames: {error}",
                path=str(canonical_dir),
            ),
        )

    findings: list[PreflightFinding] = []
    for source, target in suggestions:
        findings.append(
            _finding(
                "red",
                "canonical-session-id",
                f"non-canonical session filename: {source}",
                path=str(canonical_dir / source),
                detail=f"rename to {target}",
            )
        )
    return tuple(findings)


def _sessions_for_stable_hash(
    snapshot: PlanSnapshot,
) -> tuple[SessionFile, ...]:
    return tuple(status.session for status in snapshot.sessions)


def _check_manifest_readiness_block(
    snapshot: PlanSnapshot,
) -> tuple[tuple[PreflightFinding, ...], ReadinessMetadata | None, str]:
    sessions = _sessions_for_stable_hash(snapshot)
    expected_hash = stable_readiness_sha256(snapshot.plan_dir, sessions)

    try:
        manifest_text = snapshot.manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        finding = _finding(
            "red",
            "manifest-readiness",
            f"cannot read manifest.md: {error}",
            path=str(snapshot.manifest_path),
        )
        return (finding,), None, expected_hash

    try:
        readiness = parse_readiness_metadata(manifest_text)
    except PlanError as error:
        finding = _finding(
            "red",
            "manifest-readiness",
            f"manifest readiness metadata is malformed: {error}",
            path=str(snapshot.manifest_path),
        )
        return (finding,), None, expected_hash

    findings: list[PreflightFinding] = []

    if readiness is None:
        findings.append(
            _finding(
                "red",
                "manifest-readiness",
                "manifest is missing readiness metadata",
                path=str(snapshot.manifest_path),
                detail=(
                    "add **Readiness**, **Readiness SHA-256**, and "
                    "**Readiness checked at** lines"
                ),
            )
        )
        return tuple(findings), None, expected_hash

    if readiness.readiness_sha256 != expected_hash:
        findings.append(
            _finding(
                "red",
                "manifest-readiness",
                "manifest readiness SHA-256 does not match current plan content",
                path=str(snapshot.manifest_path),
                detail=(
                    f"recorded {readiness.readiness_sha256}; "
                    f"expected {expected_hash}"
                ),
            )
        )

    if readiness.state in {"draft", "reviewed"}:
        findings.append(
            _finding(
                "yellow",
                "manifest-readiness",
                f"plan readiness state is {readiness.state!r}",
                path=str(snapshot.manifest_path),
                detail="run preflight to advance to loop-ready when checks pass",
            )
        )

    return tuple(findings), readiness, expected_hash


def _instructions_range(lines: list[str]) -> tuple[int, int]:
    handoff_start = _final_handoff_start(lines)
    end = handoff_start if handoff_start is not None else len(lines)
    return 0, end


def _check_session_contracts(
    snapshot: PlanSnapshot,
) -> tuple[tuple[PreflightFinding, ...], dict[str, CompletionContract]]:
    findings: list[PreflightFinding] = []
    contracts: dict[str, CompletionContract] = {}

    for status in snapshot.sessions:
        session = status.session
        text = _read_session_text(session)
        if text is None:
            findings.append(
                _finding(
                    "red",
                    "missing-contract",
                    f"cannot read session file: {session.path}",
                    session_id=session.id,
                    path=str(session.path),
                )
            )
            continue

        try:
            contract = parse_completion_contract(text, session_id=session.session_id)
        except PlanError as error:
            findings.append(
                _finding(
                    "red",
                    "malformed-contract",
                    f"session {session.id} completion contract is malformed: {error}",
                    session_id=session.id,
                    path=str(session.path),
                )
            )
            continue

        if contract is None:
            findings.append(
                _finding(
                    "red",
                    "missing-contract",
                    f"session {session.id} is missing a {COMPLETION_CONTRACT_HEADING} section",
                    session_id=session.id,
                    path=str(session.path),
                    detail=(
                        "add Done when bullets and a fenced ```json``` Verify recipe"
                    ),
                )
            )
            continue

        contracts[session.id] = contract

    return tuple(findings), contracts


def _check_ambiguity_sentinels(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for status in snapshot.sessions:
        session = status.session
        text = _read_session_text(session)
        if text is None:
            continue
        lines = text.splitlines()
        start, end = _instructions_range(lines)
        for line_index in range(start, end):
            line = lines[line_index]
            sentinel = _matching_ambiguity_sentinel(line)
            if sentinel is not None:
                findings.append(
                    _finding(
                        "yellow",
                        "ambiguity-sentinel",
                        f"session {session.id} contains ambiguity sentinel {sentinel!r}",
                        session_id=session.id,
                        path=str(session.path),
                        line=line_index + 1,
                        detail=line.strip()[:200],
                    )
                )

    return tuple(findings)


def _matching_ambiguity_sentinel(line: str) -> str | None:
    word_match = WORD_AMBIGUITY_SENTINEL_PATTERN.search(line)
    if word_match is not None:
        return word_match.group(1)

    for sentinel in LITERAL_AMBIGUITY_SENTINELS:
        if sentinel in line:
            return sentinel

    return None


def _check_path_references(
    contracts_by_session: dict[str, CompletionContract],
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    for session_id in sorted(contracts_by_session):
        contract = contracts_by_session[session_id]
        for check in contract.verify.checks:
            path_value: str | None = None
            if isinstance(check, FileExistsCheck):
                path_value = check.path
            elif isinstance(check, RegexInFileCheck):
                path_value = check.path
            else:
                continue

            candidate = Path(path_value)
            if candidate.is_absolute():
                findings.append(
                    _finding(
                        "yellow",
                        "path-reference",
                        (
                            f"session {session_id} check {check.name!r} uses an "
                            "absolute path"
                        ),
                        session_id=session_id,
                        detail=f"path: {path_value}",
                    )
                )
                continue

            normalized_parts = tuple(candidate.parts)
            if ".." in normalized_parts:
                findings.append(
                    _finding(
                        "yellow",
                        "path-reference",
                        (
                            f"session {session_id} check {check.name!r} escapes "
                            "the repo root with '..'"
                        ),
                        session_id=session_id,
                        detail=f"path: {path_value}",
                    )
                )

    return tuple(findings)


def _format_progress_session_ids(session_ids: tuple[str, ...]) -> str:
    if not session_ids:
        return "none"
    return ", ".join(session_ids)


def _progress_journal_mismatch_detail(
    expected_completed_ids: tuple[str, ...],
    entries: tuple[ProgressEntry, ...],
) -> str:
    observed_ids = tuple(entry.session_id.value for entry in entries)
    details: list[str] = []
    if observed_ids != expected_completed_ids:
        details.append(
            "expected complete sessions: "
            f"{_format_progress_session_ids(expected_completed_ids)}; "
            "observed journal entries: "
            f"{_format_progress_session_ids(observed_ids)}"
        )
    non_complete = tuple(
        f"{entry.session_id.value}={entry.status}"
        for entry in entries
        if entry.status != "complete"
    )
    if non_complete:
        details.append(
            "non-complete journal entry statuses: " + ", ".join(non_complete)
        )
    return "; ".join(details)


def _check_progress_journal(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    """Check that the runner-managed progress journal is initialized and current.

    Emits a single ``yellow`` finding when at least one session is complete
    and the journal is missing, or when the journal exists but is
    unreadable (version mismatch, malformed), stale, duplicated,
    out-of-order, or missing completed sessions. Plans with no completed
    sessions and no journal are skipped because the runner will create the
    journal on the next successful append.
    """
    journal_path = progress_path(snapshot.plan_dir)
    expected_completed_ids = tuple(
        status.id for status in snapshot.sessions if status.status == "complete"
    )

    if not journal_path.is_file():
        if not expected_completed_ids:
            return ()
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                "progress journal not initialized; run `asl backfill-progress`",
                path=str(journal_path),
            ),
        )

    try:
        entries = read_progress_entries(snapshot.plan_dir)
    except PlanError as error:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal unreadable; run "
                    f"`asl backfill-progress --force`: {error}"
                ),
                path=str(journal_path),
            ),
        )
    mismatch_detail = _progress_journal_mismatch_detail(
        expected_completed_ids,
        entries,
    )
    if mismatch_detail:
        return (
            _finding(
                "yellow",
                "progress-journal-missing",
                (
                    "progress journal stale or incomplete; run "
                    "`asl backfill-progress --force`"
                ),
                path=str(journal_path),
                detail=mismatch_detail,
            ),
        )
    return ()


def run_core_preflight(plan_dir: str | Path) -> PreflightReport:
    """Run the core preflight checks against a split plan directory.

    Structural failures short-circuit deeper checks: if the plan directory is
    missing or unreadable, only a ``plan-structure`` finding is returned. When
    structure validates, the report also includes canonical session-id checks,
    manifest readiness metadata, per-session completion contracts, ambiguity
    sentinels, and lightweight contract path-reference checks. Severity is
    surfaced via ``PreflightFinding.severity``; ``PreflightReport.ok`` is
    ``True`` only when every finding is green.
    """
    canonical_dir = canonicalize_path(plan_dir)
    findings: list[PreflightFinding] = []

    structure_findings, snapshot = _check_plan_structure(canonical_dir)
    findings.extend(structure_findings)

    if not canonical_dir.is_dir():
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
        )

    canonical_findings = _check_canonical_session_ids(canonical_dir)
    findings.extend(canonical_findings)

    if snapshot is None:
        return PreflightReport(
            plan_dir=canonical_dir,
            findings=tuple(findings),
        )

    readiness_findings, readiness, expected_hash = _check_manifest_readiness_block(
        snapshot
    )
    findings.extend(readiness_findings)

    contract_findings, contracts_by_session = _check_session_contracts(snapshot)
    findings.extend(contract_findings)

    findings.extend(_check_ambiguity_sentinels(snapshot))
    findings.extend(_check_path_references(contracts_by_session))
    findings.extend(_check_progress_journal(snapshot))

    return PreflightReport(
        plan_dir=canonical_dir,
        findings=tuple(findings),
        snapshot=snapshot,
        readiness=readiness,
        expected_readiness_sha256=expected_hash,
        contracts_by_session=contracts_by_session,
    )


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None


def _check_env_repo_root(cwd: Path) -> tuple[PreflightFinding, ...]:
    if not cwd.exists():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd does not exist: {cwd}",
                path=str(cwd),
            ),
        )
    if not cwd.is_dir():
        return (
            _finding(
                "red",
                "env-repo-root",
                f"cwd is not a directory: {cwd}",
                path=str(cwd),
            ),
        )
    return ()


def _check_env_git(
    cwd: Path,
    commit_mode: CommitMode | None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    inside = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"])
    if inside is None:
        return (
            _finding(
                "red",
                "env-git",
                "git executable not available on PATH",
                path=str(cwd),
            ),
        )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        detail = (inside.stderr.strip() or inside.stdout.strip())[:200]
        return (
            _finding(
                "red",
                "env-git",
                f"cwd is not inside a git repository: {cwd}",
                path=str(cwd),
                detail=detail,
            ),
        )

    if commit_mode in COMMIT_MODES:
        status = _run_git(cwd, ["status", "--porcelain"])
        if status is None or status.returncode != 0:
            detail = ""
            if status is not None:
                detail = (status.stderr.strip() or status.stdout.strip())[:200]
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "could not inspect git status",
                    path=str(cwd),
                    detail=detail,
                )
            )
        elif status.stdout:
            findings.append(
                _finding(
                    "yellow",
                    "env-git",
                    "git worktree is dirty; commit-mode runs require a clean tree",
                    path=str(cwd),
                    detail=f"commit-mode: {commit_mode}",
                )
            )

    return tuple(findings)


def _check_env_harness(harness: str) -> tuple[PreflightFinding, ...]:
    if harness not in harnesses.HARNESS_NAMES:
        return (
            _finding(
                "red",
                "env-harness",
                f"unsupported harness: {harness!r}",
                detail=f"choices: {', '.join(harnesses.HARNESS_NAMES)}",
            ),
        )
    try:
        adapter = harnesses.get_adapter(harness)
    except harnesses.HarnessError as error:
        return (
            _finding(
                "red",
                "env-harness",
                f"cannot construct harness adapter for {harness!r}: {error}",
            ),
        )

    resolved = adapter.resolve_executable()
    if resolved is None:
        return (
            _finding(
                "red",
                "env-harness",
                f"{harness} executable not found on PATH",
                detail=f"executable: {adapter.executable}",
            ),
        )
    return ()


def _check_env_spec(commit_mode: CommitMode) -> tuple[PreflightFinding, ...]:
    if shutil.which("spec") is None:
        return (
            _finding(
                "red",
                "env-spec",
                (
                    "spec executable not found on PATH "
                    f"(required for commit-mode {commit_mode!r})"
                ),
                detail="install spec or omit --commit-per-session/--commit-at-end",
            ),
        )
    return ()


def _check_env_lock(
    plan_dir: Path,
    state_dir: str | Path | None,
) -> tuple[PreflightFinding, ...]:
    try:
        lock_info = state.read_lock_info(plan_dir, state_dir)
    except OSError as error:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"could not read lock info: {error}",
                path=str(plan_dir),
            ),
        )
    if lock_info is None:
        return ()
    if lock_info.stale:
        return (
            _finding(
                "yellow",
                "env-lock",
                f"stale lock present at {lock_info.path}",
                path=str(lock_info.path),
                detail=lock_info.reason,
            ),
        )
    return (
        _finding(
            "red",
            "env-lock",
            f"active lock held by pid {lock_info.pid} at {lock_info.path}",
            path=str(lock_info.path),
            detail=lock_info.reason,
        ),
    )


def _check_env_amx() -> tuple[PreflightFinding, ...]:
    amx_path = shutil.which("amx")
    if amx_path is None:
        return (
            _finding(
                "green",
                "env-amx",
                "amx hook indicator not on PATH (Agent Mux hooks disabled)",
                detail="optional",
            ),
        )
    return (
        _finding(
            "green",
            "env-amx",
            f"amx hook indicator available at {amx_path}",
            detail="Agent Mux hooks enabled",
        ),
    )


def _check_env_turn_budget(
    snapshot: PlanSnapshot,
) -> tuple[PreflightFinding, ...]:
    total = len(snapshot.sessions)
    pending = sum(1 for status in snapshot.sessions if status.status == "pending")
    if pending == 0:
        return (
            _finding(
                "green",
                "env-turn-budget",
                "all sessions complete; no turns required",
                detail=f"total sessions: {total}",
            ),
        )
    estimated_turns = pending * TURNS_PER_SESSION_ESTIMATE
    if pending >= TURN_BUDGET_YELLOW_THRESHOLD:
        return (
            _finding(
                "yellow",
                "env-turn-budget",
                (
                    f"{pending} pending sessions exceeds yellow threshold "
                    f"{TURN_BUDGET_YELLOW_THRESHOLD}; consider chunking with "
                    "--max-sessions"
                ),
                detail=(
                    f"total sessions: {total}; rough turn budget "
                    f"~{estimated_turns} ({TURNS_PER_SESSION_ESTIMATE} turns/session)"
                ),
            ),
        )
    return (
        _finding(
            "green",
            "env-turn-budget",
            (
                f"{pending} session(s) pending; rough turn budget "
                f"~{estimated_turns} turns"
            ),
            detail=(
                f"total sessions: {total}; "
                f"{TURNS_PER_SESSION_ESTIMATE} turns/session estimate; "
                f"yellow threshold {TURN_BUDGET_YELLOW_THRESHOLD} pending"
            ),
        ),
    )


def _run_environment_checks(
    *,
    plan_dir: Path,
    snapshot: PlanSnapshot | None,
    cwd: str | Path | None,
    harness: str | None,
    commit_mode: CommitMode | None,
    state_dir: str | Path | None,
) -> tuple[PreflightFinding, ...]:
    findings: list[PreflightFinding] = []

    if cwd is not None:
        canonical_cwd = canonicalize_path(cwd)
        repo_findings = _check_env_repo_root(canonical_cwd)
        findings.extend(repo_findings)
        if not repo_findings:
            findings.extend(_check_env_git(canonical_cwd, commit_mode))

    if harness is not None:
        findings.extend(_check_env_harness(harness))

    if commit_mode is not None:
        findings.extend(_check_env_spec(commit_mode))

    findings.extend(_check_env_lock(plan_dir, state_dir))
    findings.extend(_check_env_amx())

    if snapshot is not None:
        findings.extend(_check_env_turn_budget(snapshot))

    return tuple(findings)


def run_environment_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
) -> PreflightReport:
    """Run only the environment preflight checks against a plan directory.

    Environment checks are compositional: ``cwd``, ``harness``, and
    ``commit_mode`` are all optional, and each one only triggers the
    checks that depend on it. Lock, ``amx``, and turn-budget checks run
    whenever the plan directory exists. Core plan checks (structure,
    contracts, readiness) are intentionally **not** invoked here; use
    ``run_preflight`` to aggregate both.
    """
    canonical_dir = canonicalize_path(plan_dir)
    snapshot: PlanSnapshot | None = None
    if canonical_dir.is_dir():
        try:
            snapshot = inspect_plan(canonical_dir)
        except PlanError:
            snapshot = None

    env_findings = _run_environment_checks(
        plan_dir=canonical_dir,
        snapshot=snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
    )
    return PreflightReport(
        plan_dir=canonical_dir,
        findings=env_findings,
        snapshot=snapshot,
    )


def run_preflight(
    plan_dir: str | Path,
    *,
    cwd: str | Path | None = None,
    harness: str | None = None,
    commit_mode: CommitMode | None = None,
    state_dir: str | Path | None = None,
) -> PreflightReport:
    """Aggregate core and environment preflight findings into one report.

    Core findings are produced first via ``run_core_preflight`` and reuse
    the same plan snapshot when feeding the environment checks, so
    ``inspect_plan`` runs at most once per call.
    """
    core_report = run_core_preflight(plan_dir)
    env_findings = _run_environment_checks(
        plan_dir=core_report.plan_dir,
        snapshot=core_report.snapshot,
        cwd=cwd,
        harness=harness,
        commit_mode=commit_mode,
        state_dir=state_dir,
    )
    return PreflightReport(
        plan_dir=core_report.plan_dir,
        findings=core_report.findings + env_findings,
        snapshot=core_report.snapshot,
        readiness=core_report.readiness,
        expected_readiness_sha256=core_report.expected_readiness_sha256,
        contracts_by_session=core_report.contracts_by_session,
    )


PREFLIGHT_EXIT_REASON_OK = "ok"
PREFLIGHT_EXIT_REASON_RED = "preflight-red"
PREFLIGHT_EXIT_REASON_YELLOW = "preflight-yellow"
PREFLIGHT_EXIT_REASON_MARKED = "loop-ready-marked"
PREFLIGHT_EXIT_REASON_MARK_REFUSED = "loop-ready-refused"
PREFLIGHT_EXIT_REASON_MARK_FAILED = "loop-ready-write-failed"


def _finding_payload(finding: PreflightFinding) -> dict[str, Any]:
    return asdict(finding)


def _readiness_payload(readiness: ReadinessMetadata | None) -> dict[str, Any] | None:
    if readiness is None:
        return None
    return asdict(readiness)


def _counts_by_severity(report: PreflightReport) -> dict[str, int]:
    return {
        "red": len(report.red_findings),
        "yellow": len(report.yellow_findings),
        "green": len(report.green_findings),
    }


def _primary_error_reason(report: PreflightReport) -> str:
    if report.red_findings:
        return PREFLIGHT_EXIT_REASON_RED
    return PREFLIGHT_EXIT_REASON_YELLOW


def _preflight_payload_fields(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    state_dir: str | None,
    mark_loop_ready: bool,
    marked: bool | None = None,
    mark_error: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "plan_dir": plan_dir_arg,
        "plan_dir_resolved": str(report.plan_dir),
        "cwd": cwd,
        "harness": harness,
        "commit_mode": commit_mode,
        "state_dir": state_dir,
        "ok": report.ok,
        "findings": [_finding_payload(finding) for finding in report.findings],
        "findings_by_severity": _counts_by_severity(report),
        "readiness": _readiness_payload(report.readiness),
        "expected_readiness_sha256": report.expected_readiness_sha256,
        "mark_loop_ready_requested": mark_loop_ready,
        "marked_loop_ready": bool(marked) if marked is not None else False,
        "repair_hints": [asdict(hint) for hint in report.repair_hints],
    }
    if mark_error is not None:
        fields["mark_error"] = mark_error
    return fields


def _format_finding_text(finding: PreflightFinding) -> str:
    severity_marker = f"[{finding.severity.upper()}]"
    session_label = f" {finding.session_id}" if finding.session_id else ""
    line = f"  {severity_marker} {finding.category}{session_label}: {finding.message}"
    if finding.path:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        line += f" ({location})"
    return line


def _print_text_preflight(
    report: PreflightReport,
    *,
    plan_dir_arg: str,
    cwd: str | None,
    harness: str | None,
    commit_mode: str | None,
    marked: bool,
    mark_loop_ready: bool,
    mark_error: str | None,
) -> None:
    counts = _counts_by_severity(report)
    session_count = (
        len(report.snapshot.sessions) if report.snapshot is not None else 0
    )
    print(f"Plan: {plan_dir_arg}")
    print(f"Sessions: {session_count}")
    if cwd is not None:
        print(f"Repo root: {cwd}")
    if harness is not None:
        print(f"Harness: {harness}")
    if commit_mode is not None:
        print(f"Commit mode: {commit_mode}")
    print(
        f"Findings: {len(report.findings)} "
        f"(red {counts['red']}, yellow {counts['yellow']}, green {counts['green']})"
    )
    for finding in report.findings:
        print(_format_finding_text(finding))
    if report.ok:
        print("Preflight: ok")
    else:
        print(
            f"Preflight: failed ({counts['red']} red, {counts['yellow']} yellow)",
            file=sys.stderr,
        )
    if mark_loop_ready:
        if marked:
            print("Loop-ready: marked")
        elif mark_error is not None:
            print(f"Loop-ready: not marked ({mark_error})", file=sys.stderr)
        else:
            print(
                "Loop-ready: refused (preflight not ok)",
                file=sys.stderr,
            )


def _mark_loop_ready(
    report: PreflightReport,
) -> tuple[bool, str | None]:
    if report.expected_readiness_sha256 is None:
        return False, "no readiness sha-256 was computed; cannot mark loop-ready"
    metadata = ReadinessMetadata(
        state="loop-ready",
        readiness_sha256=report.expected_readiness_sha256,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        write_readiness_metadata(report.plan_dir, metadata)
    except PlanError as error:
        return False, str(error)
    return True, None


def _mark_loop_ready_blockers(
    report: PreflightReport,
) -> tuple[PreflightFinding, ...]:
    """Return findings that must be fixed before readiness can be marked.

    Manifest-readiness findings are precisely what ``--mark-loop-ready`` repairs:
    missing metadata, stale SHA values, and draft/reviewed states. All other red
    or yellow findings still block marking.
    """
    return tuple(
        finding
        for finding in report.findings
        if finding.severity in {"red", "yellow"}
        and finding.category != "manifest-readiness"
    )


def _mark_loop_ready_refusal_message(
    blockers: tuple[PreflightFinding, ...],
) -> str:
    red = sum(1 for finding in blockers if finding.severity == "red")
    yellow = sum(1 for finding in blockers if finding.severity == "yellow")
    categories = ", ".join(sorted({finding.category for finding in blockers}))
    return (
        f"preflight still has {red} red and {yellow} yellow "
        f"non-readiness finding(s): {categories}"
    )


def preflight_from_args(args: argparse.Namespace) -> int:
    """Run the ``preflight`` subcommand from CLI arguments."""
    plan_dir_arg = args.plan_dir
    cwd_arg: str | None = args.cwd
    harness_arg: str | None = args.harness
    commit_mode_arg: str | None = args.commit_mode
    state_dir_arg: str | None = args.state_dir
    want_json: bool = args.json
    mark_loop_ready: bool = args.mark_loop_ready

    try:
        report = run_preflight(
            plan_dir_arg,
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            state_dir=state_dir_arg,
        )
    except PlanError as error:
        if want_json:
            responses.print_json(
                responses.error_payload(
                    "preflight",
                    reason="plan-structure",
                    error=str(error),
                    paused=False,
                    plan_dir=plan_dir_arg,
                    cwd=cwd_arg,
                    harness=harness_arg,
                    commit_mode=commit_mode_arg,
                    state_dir=state_dir_arg,
                )
            )
        else:
            print(
                f"agent-session-loop preflight: {error}",
                file=sys.stderr,
            )
        return 1

    marked = False
    mark_error: str | None = None
    mark_write_failed = False
    if mark_loop_ready:
        blockers = _mark_loop_ready_blockers(report)
        if blockers:
            mark_error = _mark_loop_ready_refusal_message(blockers)
        else:
            marked, mark_error = _mark_loop_ready(report)
            mark_write_failed = mark_error is not None
            if marked:
                try:
                    report = run_preflight(
                        plan_dir_arg,
                        cwd=cwd_arg,
                        harness=harness_arg,
                        commit_mode=commit_mode_arg,
                        state_dir=state_dir_arg,
                    )
                except PlanError as error:
                    mark_error = str(error)
                    mark_write_failed = True

    payload_fields = _preflight_payload_fields(
        report,
        plan_dir_arg=str(plan_dir_arg),
        cwd=cwd_arg,
        harness=harness_arg,
        commit_mode=commit_mode_arg,
        state_dir=state_dir_arg,
        mark_loop_ready=mark_loop_ready,
        marked=marked,
        mark_error=mark_error,
    )

    if want_json:
        if report.ok:
            if mark_loop_ready and not marked:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARK_FAILED
                    if mark_error is not None
                    else PREFLIGHT_EXIT_REASON_MARK_REFUSED
                )
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=reason,
                        error=mark_error
                        or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                reason = (
                    PREFLIGHT_EXIT_REASON_MARKED
                    if marked
                    else PREFLIGHT_EXIT_REASON_OK
                )
                responses.print_json(
                    responses.success_payload(
                        "preflight",
                        reason=reason,
                        **payload_fields,
                    )
                )
        else:
            if mark_loop_ready and mark_write_failed:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=PREFLIGHT_EXIT_REASON_MARK_FAILED,
                        error=mark_error
                        or "loop-ready marking did not complete",
                        paused=False,
                        **payload_fields,
                    )
                )
            else:
                responses.print_json(
                    responses.error_payload(
                        "preflight",
                        reason=_primary_error_reason(report),
                        error=(
                            f"{len(report.red_findings)} red, "
                            f"{len(report.yellow_findings)} yellow finding(s)"
                        ),
                        paused=False,
                        **payload_fields,
                    )
                )
    else:
        _print_text_preflight(
            report,
            plan_dir_arg=str(plan_dir_arg),
            cwd=cwd_arg,
            harness=harness_arg,
            commit_mode=commit_mode_arg,
            marked=marked,
            mark_loop_ready=mark_loop_ready,
            mark_error=mark_error,
        )

    if not report.ok:
        return 1
    if mark_loop_ready and not marked:
        return 1
    return 0
