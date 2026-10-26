"""Audit the runs whose own process died before it could audit them.

A run normally audits itself at terminalization. A killed runner never gets
there, so :func:`agent_session_loop.state.mark_run_abandoned` records that the
audit is still owed and this module runs it later, out of band. The split is
deliberate: abandonment is detected inside ``asl status`` and inside lock
acquisition, and neither may launch a harness or wait on one.

The record is explicit and self-contained. Only a run this feature marked is
ever a candidate, so a state root full of pre-audit history cannot be
discovered and dumped into the shared inbox; and the dead run's harness
selection, workspace binding, and plan directory are copied into the record,
because the next run of the same plan overwrites ``state.json`` and would
otherwise take them with it.

A bulk pass is also bounded to the current code generation. A run recorded
before the newest issue resolution ran under code the fix has since replaced, so
grading it can only re-report the defect that resolution already answered — and
that is precisely how a backlog reopens issues an operator closed. Those records
are reported as skipped rather than deleted, and naming one by run id still
audits it.

Processing is serial and single-winner. Each run is claimed with a nonblocking
``flock`` in its own run directory, so two catch-up processes divide the work
instead of racing on it, and the ledger is consulted inside that claim, so a
run already published is never published twice. Nothing here rewrites the
abandoned run's own outcome: it died abandoned and stays abandoned, whatever
its audit later says.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import assurance_projection as projection
from . import (
    clock,
    events,
    plans_files,
    state,
    state_assurance_pending,
    state_plan_refs,
    supervisor_audit,
)
from .assurance_models_primitives import AssuranceError
from .assurance_models_vocab import ATTEMPT_DIR_PREFIX, CANONICAL_AUDIT_ARTIFACTS

#: Runs audited per invocation unless the operator raises it. Each audit is a
#: harness turn under a timeout, so the default bounds one catch-up pass to
#: something an operator can sit through rather than to the whole backlog.
DEFAULT_PENDING_LIMIT = 20

#: Sidecar ``flock`` target that makes processing one run single-winner. It
#: lives in the run directory so two catch-up passes over the same state root
#: contend per run rather than globally.
CLAIM_FILE_NAME = "assurance-catchup.lock"

OUTCOME_AUDITED = "audited"
OUTCOME_ALREADY_AUDITED = "already-audited"
OUTCOME_SKIPPED = "skipped"
OUTCOME_CLAIMED = "claimed"
OUTCOME_UNUSABLE = "unusable"

#: The plan's ``state.json`` has moved on to a later run, so the evidence an
#: audit would read no longer describes the run it is owed for.
REASON_EVIDENCE_SUPERSEDED = "evidence-superseded"
#: The record itself does not name the plan directory and workspace an audit
#: needs, so there is nothing to audit against.
REASON_MALFORMED_RECORD = "malformed-record"
#: The recorded run configuration cannot resolve an audit role.
REASON_AUDIT_CONFIG_UNAVAILABLE = "audit-config-unavailable"
#: The run ended before the newest issue resolution, so its evidence was
#: produced by code that predates the fix a bulk pass would re-grade it against.
REASON_SUPERSEDED_GENERATION = "superseded-generation"

REASON_UNKNOWN_RUN = "unknown-run"
REASON_AMBIGUOUS_RUN = "ambiguous-run"

#: Where a dead runner's half-written audit artifacts are moved so the
#: re-audit can write the canonical names itself.
ABANDONED_AUDIT_ARCHIVE_PREFIX = "abandoned-audit-"

#: The ``run_config`` keys that reconstruct the dead run's audit decision.
#: Names are ``RUN_FLAG_SPEC`` dests, which is what ``plan_audit`` reads.
_AUDIT_NAMESPACE_KEYS: tuple[str, ...] = (
    "harness",
    "review_harness",
    "review_harness_model",
    "supervisor_audit",
    "audit_harness",
    "audit_harness_model",
    "audit_effort",
    "audit_timeout",
)


class CatchupError(Exception):
    """Raised when a catch-up request names something that cannot be processed."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AuditOverrides:
    """Operator-supplied audit role settings that beat the recorded ones."""

    harness: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_seconds: int | None = None


NO_AUDIT_OVERRIDES = AuditOverrides()


@dataclass(frozen=True)
class PendingRun:
    """One recorded audit an abandoned run still owes."""

    run_id: str
    record_path: Path
    state_dir: Path
    run_dir: Path
    plan_dir: Path | None = None
    workspace_root: Path | None = None
    recorded_at: str | None = None
    trigger: str | None = None
    skip: str | None = None
    run_config: Mapping[str, Any] = field(default_factory=dict)

    @property
    def plan_id(self) -> str | None:
        return None if self.plan_dir is None else self.plan_dir.name


@dataclass(frozen=True)
class CatchupResult:
    """What one catch-up pass did about one pending run."""

    run_id: str
    outcome: str
    plan_id: str | None = None
    state: str | None = None
    reason: str | None = None
    issue_ids: tuple[str, ...] = ()
    archived_artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "plan_id": self.plan_id,
            "state": self.state,
            "reason": self.reason,
            "issue_ids": list(self.issue_ids),
            "archived_artifacts": list(self.archived_artifacts),
        }


@dataclass(frozen=True)
class CatchupReport:
    """Everything one catch-up pass considered, in the order it considered it."""

    results: tuple[CatchupResult, ...]
    candidate_count: int
    limit: int
    limit_reached: bool

    @property
    def audited(self) -> tuple[CatchupResult, ...]:
        return tuple(item for item in self.results if item.outcome == OUTCOME_AUDITED)


def record_path(run_dir: Path | str) -> Path:
    """Return where one run's pending-audit record lives."""
    return Path(run_dir) / state_assurance_pending.ASSURANCE_PENDING_FILE_NAME


def _path_or_none(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return plans_files.canonicalize_path(value)
    except OSError, ValueError:
        return None


def read_pending_run(run_dir: Path | str) -> PendingRun | None:
    """Read one run's pending-audit record, or ``None`` when it has none.

    A record that cannot be parsed reads as absent rather than as an error: it
    describes an audit nobody has run yet, and refusing to enumerate the rest
    of the corpus over one damaged file would make the whole backlog
    unreachable.
    """
    path = record_path(run_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    run_id = parsed.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    config = parsed.get("run_config")
    return PendingRun(
        run_id=run_id,
        record_path=path,
        state_dir=Path(run_dir).parent.parent,
        run_dir=Path(run_dir),
        plan_dir=_path_or_none(parsed.get("plan_dir")),
        workspace_root=_path_or_none(parsed.get("workspace_root")),
        recorded_at=parsed.get("recorded_at"),
        trigger=parsed.get("trigger"),
        skip=parsed.get("skip"),
        run_config=config if isinstance(config, dict) else {},
    )


def _run_dirs(plan_state_dir: Path, run_id: str | None) -> list[Path]:
    runs_dir = plan_state_dir / state.RUNS_DIR_NAME
    if run_id is not None:
        candidate = runs_dir / run_id
        return [candidate] if candidate.is_dir() else []
    try:
        return sorted(item for item in runs_dir.iterdir() if item.is_dir())
    except OSError:
        return []


def iter_pending_runs(
    *,
    state_dir: str | Path | None = None,
    plan_dir: str | Path | None = None,
    run_id: str | None = None,
) -> list[PendingRun]:
    """Return every recorded pending audit, oldest sighting first.

    Only records this feature wrote are visible, so a state root of runs that
    predate auditing enumerates as empty rather than as a backlog.
    """
    if run_id is not None and (
        not run_id or run_id in {".", ".."} or "/" in run_id or os.sep in run_id
    ):
        return []
    root = state.state_root(state_dir)
    if plan_dir is not None:
        plan_state_dirs = [state.plan_state_dir(plan_dir, state_dir)]
    else:
        plan_state_dirs = list(state_plan_refs.iter_plan_state_dirs(root))
    found: list[PendingRun] = []
    for plan_state_dir in plan_state_dirs:
        for run_directory in _run_dirs(plan_state_dir, run_id):
            pending = read_pending_run(run_directory)
            if pending is not None:
                found.append(pending)
    found.sort(key=lambda item: (item.recorded_at or "", item.run_id))
    return found


@contextmanager
def _claim(run_dir: Path) -> Iterator[bool]:
    """Hold the exclusive claim on auditing one run, or yield ``False``.

    Nonblocking on purpose: losing the claim means another catch-up process is
    auditing this run right now, and an audit is a harness turn under a
    timeout, so waiting would stall the whole serial pass behind it.
    """
    fd: int | None = None
    held = False
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(run_dir / CLAIM_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError:
        pass
    try:
        yield held
    finally:
        if fd is not None:
            if held:
                with suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _audit_namespace(
    pending: PendingRun, *, overrides: AuditOverrides, force_enabled: bool
) -> argparse.Namespace:
    values: dict[str, Any] = {
        key: pending.run_config.get(key) for key in _AUDIT_NAMESPACE_KEYS
    }
    if overrides.harness is not None:
        values["audit_harness"] = overrides.harness
    if overrides.model is not None:
        values["audit_harness_model"] = overrides.model
    if overrides.effort is not None:
        values["audit_effort"] = overrides.effort
    if overrides.timeout_seconds is not None:
        values["audit_timeout"] = overrides.timeout_seconds
    if force_enabled:
        values["supervisor_audit"] = True
    return argparse.Namespace(**values)


def _state_still_describes(pending: PendingRun) -> bool:
    """Report whether the plan's live state is still this run's own history."""
    state_path = pending.state_dir / events.STATE_FILE_NAME
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("run_id") == pending.run_id


def generation_watermark(inbox: projection.AssuranceProjection) -> str | None:
    """When the code these audits grade last changed, per the ledger itself.

    A resolution is the operator's statement that a defect is fixed, which is
    the only disposition that claims the code moved: dismissing an issue or
    accepting its risk both leave the behavior exactly as the audits found it.
    ``None`` when nothing has ever been resolved, which puts every record in the
    current generation rather than inventing a boundary.
    """
    resolved = [
        disposition.recorded_at
        for issue in inbox.issues
        for disposition in issue.dispositions
        if disposition.kind == "resolve"
    ]
    return max(resolved, key=_moment, default=None)


def _moment(timestamp: str) -> datetime:
    """Order two ledger timestamps by instant rather than by spelling."""
    # Always aware, so a hand-edited naive stamp cannot make ``max`` compare
    # naive with aware and raise.
    parsed = clock.parse_datetime(timestamp)
    return parsed if parsed is not None else datetime.min.replace(tzinfo=UTC)


def _predates_generation(
    pending: PendingRun, inbox: projection.AssuranceProjection
) -> bool:
    """Report whether a bulk pass should leave this run to the older backlog.

    A run recorded before the newest resolution ran under code that no longer
    exists, so auditing it can only re-report the behavior that resolution
    already answered — which is how a resolved issue gets reopened by history.
    A record carrying no timestamp cannot be placed in any generation and is
    left in the current one, because dropping a run over a missing field would
    hide it rather than date it.
    """
    watermark = generation_watermark(inbox)
    if watermark is None or pending.recorded_at is None:
        return False
    return _moment(pending.recorded_at) < _moment(watermark)


def _registered_repositories(pending: PendingRun) -> tuple[tuple[str, ...], bool]:
    """Rebuild the dead run's registry, and say whether it recorded one at all.

    A record that names no repositories is a legacy single-repository run,
    whose one repository is the workspace root it recorded. That root is often
    not a worktree root, so the audit is told those paths are synthesized and
    must not be graded as strictly as a registry the run actually declared.
    """
    recorded = pending.run_config.get("repositories")
    if not isinstance(recorded, Sequence) or isinstance(recorded, (str, bytes)):
        return (".",), False
    paths = tuple(
        str(entry["path"])
        for entry in recorded
        if isinstance(entry, Mapping) and entry.get("path")
    )
    return (paths, True) if paths else ((".",), False)


def _archive_abandoned_audit_artifacts(run_dir: Path) -> tuple[str, ...]:
    """Move a dead runner's half-written audit artifacts aside.

    The canonical writers create exclusively, so a run that died *after* its
    own audit began can never be re-audited in place: the bundle write raises
    "written exactly once", `audit_terminal_run` folds that into a pending
    summary, and catch-up reports an audit that wrote nothing. Renaming the
    leftovers into a numbered archive keeps them as evidence of the abandoned
    attempt — they are never deleted — while freeing the canonical names.

    Returns the archived names, empty when the run left nothing behind.
    """
    leftovers = [
        path
        for path in (
            [run_dir / name for name in CANONICAL_AUDIT_ARTIFACTS]
            + sorted(run_dir.glob(f"{ATTEMPT_DIR_PREFIX}*"))
        )
        if path.exists()
    ]
    if not leftovers:
        return ()
    index = 1
    while (archive := run_dir / f"{ABANDONED_AUDIT_ARCHIVE_PREFIX}{index}").exists():
        index += 1
    archive.mkdir(parents=True)
    for path in leftovers:
        path.rename(archive / path.name)
    return tuple(path.name for path in leftovers)


def _unavailable_only(published: projection.RunAudit) -> bool:
    """Whether every audit published for a run says it could not be judged.

    Such a run has a ledger record but no verdict, so re-auditing it answers a
    question nobody has answered yet rather than re-grading one already
    settled. It is still only done for a run named by id: a bulk pass that
    re-audited these would re-grade old evidence against newer code, which is
    how catch-up reopens issues an operator resolved.
    """
    summaries = published.summaries or (published.summary,)
    return all(summary.state == "unavailable" for summary in summaries)


def process_run(
    pending: PendingRun,
    *,
    state_dir: str | Path | None = None,
    overrides: AuditOverrides = NO_AUDIT_OVERRIDES,
    force: bool = False,
    **audit_kwargs: Any,
) -> CatchupResult:
    """Audit one pending run, or say why this pass left it alone."""

    def result(outcome: str, **fields: Any) -> CatchupResult:
        return CatchupResult(
            run_id=pending.run_id,
            outcome=outcome,
            plan_id=pending.plan_id,
            **fields,
        )

    if pending.skip is not None and not force:
        return result(OUTCOME_SKIPPED, reason=pending.skip)
    if pending.plan_dir is None or pending.workspace_root is None:
        return result(OUTCOME_UNUSABLE, reason=REASON_MALFORMED_RECORD)

    with _claim(pending.run_dir) as held:
        if not held:
            return result(OUTCOME_CLAIMED)
        # Read the ledger inside the claim: it is the durable record of what
        # has been published, and checking it before taking the claim would be
        # a time-of-check race with the process that holds it.
        try:
            inbox = projection.read_projection(state_dir=state_dir)
        except (AssuranceError, OSError) as error:
            return result(OUTCOME_UNUSABLE, reason=str(error))
        published = inbox.run(pending.run_id)
        if published is not None and not (force and _unavailable_only(published)):
            return result(OUTCOME_ALREADY_AUDITED)
        if not force and _predates_generation(pending, inbox):
            return result(OUTCOME_SKIPPED, reason=REASON_SUPERSEDED_GENERATION)
        if not _state_still_describes(pending):
            return result(OUTCOME_UNUSABLE, reason=REASON_EVIDENCE_SUPERSEDED)
        # Only reachable once the ledger has shown nothing was published for
        # this run, or that everything published says the audit could not be
        # judged. In the second case the archive keeps the failed attempt's
        # artifacts, which is the evidence that unavailable issue points at.
        try:
            archived = _archive_abandoned_audit_artifacts(pending.run_dir)
        except OSError as error:
            return result(OUTCOME_UNUSABLE, reason=str(error))
        try:
            plan = supervisor_audit.plan_audit(
                _audit_namespace(pending, overrides=overrides, force_enabled=force)
            )
        except AssuranceError, AttributeError, TypeError, ValueError:
            # The recorded configuration is arbitrary stored JSON, so a role
            # that cannot be rebuilt is bad input rather than a bug here.
            return result(OUTCOME_UNUSABLE, reason=REASON_AUDIT_CONFIG_UNAVAILABLE)
        repositories, registry_declared = _registered_repositories(pending)
        summary = supervisor_audit.audit_terminal_run(
            plan,
            run_id=pending.run_id,
            run_dir=pending.run_dir,
            state_dir=pending.state_dir,
            plan_dir=pending.plan_dir,
            workspace_root=pending.workspace_root,
            ledger_state_dir=state_dir,
            repositories=repositories,
            registry_declared=registry_declared,
            **audit_kwargs,
        )
    return result(
        OUTCOME_AUDITED,
        state=summary.state,
        reason=summary.reason,
        issue_ids=summary.issue_ids,
        archived_artifacts=archived,
    )


def process_pending(
    *,
    state_dir: str | Path | None = None,
    plan_dir: str | Path | None = None,
    run_id: str | None = None,
    limit: int = DEFAULT_PENDING_LIMIT,
    overrides: AuditOverrides = NO_AUDIT_OVERRIDES,
    **audit_kwargs: Any,
) -> CatchupReport:
    """Audit up to ``limit`` runs that owe one, serially, and report each.

    ``limit`` bounds the audits this pass launches, not the records it looks
    at: a run already published or deliberately skipped costs nothing and must
    not crowd out a run that still owes real work. Naming a ``run_id``
    processes that run even when its record carries a skip, predates the
    current code generation, or already has a published audit that could not
    be judged (``unavailable``). Naming it is the only way any of those is ever
    audited — never in bulk.
    """
    if limit < 1:
        raise CatchupError(
            "input-error", f"limit must be a positive integer; got {limit}"
        )
    candidates = iter_pending_runs(
        state_dir=state_dir, plan_dir=plan_dir, run_id=run_id
    )
    if run_id is not None:
        if not candidates:
            raise CatchupError(
                REASON_UNKNOWN_RUN,
                f"{run_id} has no recorded pending audit",
            )
        if len(candidates) > 1:
            raise CatchupError(
                REASON_AMBIGUOUS_RUN,
                f"{run_id} names a pending audit under {len(candidates)} plans",
            )

    results: list[CatchupResult] = []
    audited = 0
    limit_reached = False
    for pending in candidates:
        if audited >= limit:
            limit_reached = True
            break
        outcome = process_run(
            pending,
            state_dir=state_dir,
            overrides=overrides,
            force=run_id is not None,
            **audit_kwargs,
        )
        results.append(outcome)
        if outcome.outcome == OUTCOME_AUDITED:
            audited += 1
    return CatchupReport(
        results=tuple(results),
        candidate_count=len(candidates),
        limit=limit,
        limit_reached=limit_reached,
    )
