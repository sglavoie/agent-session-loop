"""What a run is admitted on, and which repositories it is allowed to reach.

Before the loop selects a single session it has to settle two independent
questions, and both of them are refusals rather than computations. The first
is whether the arguments this process was launched with describe a run that
can exist at all: a working directory that is a directory, flag values that do
not contradict each other, a Spark baseline distinct from the Spark model, an
allowlist that stays inside the repository, and a resume whose persisted
workspace binding still matches the one on disk. The second is *where* the run
would write: a plan that reaches more than one git repository without
declaring a top-level ``## Repositories`` table is a plan whose destination
nothing has bound, and a turn that hands its worker artifacts naming a second
checkout is the same defect arriving one layer later.

This module owns both, plus the small resolutions the loop reads off the same
parsed namespace afterwards -- the repair/rereview cycle cap, its absolute
ceiling, and the per-group reopen cap -- and the two plan-status projections
that decide whether a run is finished or malformed. Nothing here launches,
polls, or waits. Every function is a projection of the parsed arguments, the
plan directory, and the filesystem into a value or an exception; the loop is
left deciding *when* to admit a run rather than *how* admission stays honest.

No durable record is written here with one deliberate exception:
:func:`_apply_repository_registry` asks
:mod:`agent_session_loop.repository_registration` to derive and write the
registry the gate is refusing for. That write is the migration's own, guarded
by the migration's own refusal, and the run pauses either way -- see the
invariant below.

Six invariants are load-bearing:

* **The registry gate is the final authority.** :func:`_run_repository_intent_report`
  re-derives the verdict at run time because every earlier gate can be skipped:
  preflight by a loop-ready short circuit, prepare by a legacy plan.
  ``--allow-not-loop-ready`` waives readiness and deliberately does not waive
  this, because no readiness marking can stand in for the registry that would
  have bound a second repository in the first place.
* **A registry write never resumes the run.** :func:`_apply_repository_registry`
  writes when the migration says writing is still safe, and the caller pauses
  regardless: the rebind that write implies re-runs the prepare pipeline -- a
  validator harness and fresh baselines -- which is too large a side effect to
  trigger silently from a gate.
* **The task packet is judged on what it actually hands the worker.** The
  run-level gate judges the plan as split;
  :func:`_task_packet_repository_intent_report` reads the rendered prompt and
  every artifact it points at, because expansion notes, shared context, a
  predecessor handoff, live guidance, an operator reply, and repair references
  all reach the worker without appearing in any plan file the run-level scan
  read. Provenance is filtered exactly as the plan-level scan filters it, so a
  turn quoting where the plan came from is not a turn that redirects anywhere.
* **The runner's own trees are never the work.** :func:`_runner_owned_roots`
  exempts the plan directory, the state root, and the run directory from every
  intent scan, because an operator may legitimately keep ASL's bookkeeping
  inside an unrelated checkout and that is not a repository the run reaches.
* **An explicit plan-store root is checked, not supplied.**
  :func:`_session_plan_store_state_root_report` checks a selected contract's
  plan-store write requirement against the effective control-plane roots.
  A missing state-root override pauses before launch, never silently moves
  the run's bookkeeping or authorizes a broader tree.
* **Binding is checked, never rewritten.** :func:`_resolve_run_verification_context`
  and :func:`_validate_resume_workspace_binding` refuse a mismatch instead of
  re-deriving one, and :func:`_ensure_legacy_run_metadata` first-binds only a
  plan that has no metadata at all. A run that silently re-pointed a prepared
  workspace would pass every later gate while writing somewhere else.

The module never imports the runner. The review mutation guard's allowlist
parser is owned by ``review_guard``, and it arrives as the
:data:`ReviewGuardExcludeResolver` callback rather than as an import, which
keeps the allowlist refusal at its original position in the validation
sequence.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import (
    config,
    contracts,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_models,
    plans_payloads,
    plans_progress,
    plans_registry,
    plans_session_ids,
    preflight_footprint,
    repository_intent,
    repository_registration,
    run_flags,
    run_prompts,
    run_selection,
    state,
    verification_models,
    workspaces,
)
from .run_errors import RunError

# Review repair/rereview cycle control. After a generated review session
# completes with structured findings, the runner materializes same-plan repair
# children (``agent_session_loop.review_repair``), executes them serially, and
# appends the next top-level review session — one *cycle*. Cycles are bounded
# by ``--max-review-fix-cycles`` (default below; ``0`` still reviews but never
# creates repair sessions; ``--max-review-fix-plans`` is a deprecated alias),
# per-group reopen attempts default to two, and the operator-facing cycle cap
# cannot exceed its absolute ceiling unless that ceiling is raised too.
# Every review completion appends a ``review_cycle`` event whose fingerprints
# (:func:`review.finding_fingerprint`) let the next cycle detect repeated
# findings across processes; a repeated fingerprint stops the loop without
# another cycle.
DEFAULT_MAX_REPAIR_GROUP_ATTEMPTS = 2
DEFAULT_MAX_REVIEW_FIX_CYCLES = 4
DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING = 8

#: How the review mutation guard's allowlist is resolved while validating a run.
#:
#: :func:`_validate_run_inputs` refuses a run whose ``--review-guard-exclude``
#: values would reach outside the repository, but the parser that decides that
#: belongs to the review guard, which is still part of the loop. It arrives as
#: a callback rather than as an import so the refusal keeps happening at its
#: original point in the sequence — after the cross-field checks and the Spark
#: baseline check — instead of being hoisted ahead of them by resolving the
#: allowlist before validation starts.
ReviewGuardExcludeResolver = Callable[[argparse.Namespace], tuple[str, ...]]


def _repository_path_label(alias: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", alias).strip("-") or "repository"


def _invalid_status_pause(
    snapshot: plans_models.PlanSnapshot,
) -> tuple[str, str, dict[str, Any]] | None:
    unknown_sessions = plans_payloads.sessions_with_status(snapshot, "unknown")
    if unknown_sessions:
        return (
            "unknown-status",
            "one or more session handoff statuses are not pending or complete",
            {"sessions": unknown_sessions},
        )

    empty_sessions = plans_payloads.sessions_with_status(snapshot, "empty")
    if empty_sessions:
        return (
            "malformed-plan",
            "one or more session files are missing a populated handoff status",
            {"sessions": empty_sessions},
        )

    return None


def _all_sessions_complete(snapshot: plans_models.PlanSnapshot) -> bool:
    return all(session.status == "complete" for session in snapshot.sessions)


def _validate_run_inputs(
    args: argparse.Namespace,
    *,
    resolve_review_guard_excludes: ReviewGuardExcludeResolver,
) -> tuple[Path, int | None, int | None, str | None]:
    cwd = plans_files.canonicalize_path(args.cwd)
    if not cwd.is_dir():
        raise plans_errors.PlanError(f"cwd does not exist or is not a directory: {cwd}")

    commit_mode = run_selection._commit_mode(args)

    # The numeric/harness cross-field checks live in ``run_flags`` so the CLI
    # runner and the supervisor helper enforce identical rules; cwd existence
    # and commit-mode resolution stay here because they are CLI-runner specific.
    errors = run_flags.validate_run_namespace(
        args,
        default_max_review_fix_cycles_ceiling=DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING,
    )
    if errors:
        raise RunError(errors[0].message)

    if getattr(args, "spark", False):
        baseline = config.selected_model_payload(
            "codex", getattr(args, "harness_model", None)
        )
        if baseline["model_source"] == config.MODEL_SOURCE_UNRESOLVED:
            raise RunError(
                "--spark requires a distinct resolved baseline model; pass "
                "--harness-model or configure Codex's default model"
            )
        if baseline["model"] == run_selection.SPARK_MODEL:
            raise RunError(
                "--spark baseline must differ from the Spark model "
                f"{run_selection.SPARK_MODEL!r}"
            )

    resolve_review_guard_excludes(args)

    # Pin operator-selected data roots before any worker runs. Relative paths,
    # broad ancestors and glob patterns cannot silently widen this authority.
    external_roots: set[str] = set()
    for value in getattr(args, "external_write_root", None) or ():
        path = Path(value).expanduser()
        if not path.is_absolute() or any(char in str(path) for char in "*?[]"):
            raise RunError("--external-write-root requires an absolute directory path")
        canonical = workspaces.canonicalize_filesystem_path(path)
        if (
            value in getattr(args, "_restored_external_write_roots", ())
            and str(canonical) != value
        ):
            raise RunError(f"saved external write root changed its target: {path}")
        if not canonical.is_dir():
            raise RunError(f"external write root is not an existing directory: {path}")
        if canonical == Path.home().resolve() or cwd.is_relative_to(canonical):
            raise RunError(f"external write root is too broad: {path}")
        external_roots.add(str(canonical))
    args.external_write_root = sorted(external_roots)

    timeout = getattr(args, "timeout", None)
    max_sessions = getattr(args, "max_sessions", None)
    return cwd, timeout, max_sessions, commit_mode


def _resolve_run_verification_context(
    plan_dir: str | Path,
    cwd: Path,
) -> verification_models.VerificationContext:
    """Resolve the run root once and enforce prepare's durable binding."""
    registry = plans_registry.read_repository_registry(plan_dir)
    metadata = plans_metadata.load_plan_metadata(plan_dir)
    if registry is None:
        if isinstance(metadata, plans_metadata.WorkspacePlanMetadata):
            raise plans_errors.PlanError(
                "legacy plan has incompatible multi-repository workspace metadata"
            )
        if isinstance(metadata, plans_metadata.PlanMetadata) and metadata.cwd != str(
            cwd
        ):
            raise plans_errors.PlanError(
                f"run cwd does not match stored plan metadata: {cwd} != {metadata.cwd}"
            )
        return verification_models.VerificationContext.legacy(cwd, plan_dir=plan_dir)

    try:
        workspace = workspaces.resolve_workspace(cwd, registry)
    except workspaces.WorkspaceError as error:
        raise plans_errors.PlanError(f"invalid run workspace: {error}") from error
    if metadata is None:
        raise plans_errors.PlanError(
            "registered plan has no prepared workspace binding; run prepare first"
        )
    if not isinstance(metadata, plans_metadata.WorkspacePlanMetadata):
        raise plans_errors.PlanError(
            "registered plan has incompatible legacy workspace metadata"
        )
    if (
        metadata.workspace_root != str(workspace.root)
        or metadata.registry_digest != registry.digest
    ):
        raise plans_errors.PlanError(
            "run workspace does not match the prepared workspace binding"
        )
    return verification_models.VerificationContext.resolved_workspace(workspace)


def _validate_resume_workspace_binding(
    args: argparse.Namespace,
    context: verification_models.VerificationContext,
) -> None:
    """Reject a resume whose current binding differs from its paused run."""
    persisted = getattr(args, "_resume_workspace_config", None)
    if not isinstance(persisted, dict):
        return
    if context.workspace is None:
        if any(
            persisted.get(key) is not None
            for key in ("workspace_root", "registry_digest", "repositories")
        ):
            raise plans_errors.PlanError(
                "resume workspace mode does not match the persisted run"
            )
        return
    current = run_prompts._workspace_payload_fields(context)
    for key in ("workspace_root", "registry_digest", "repositories"):
        if persisted.get(key) != current[key]:
            raise plans_errors.PlanError(
                "resume workspace binding does not match the persisted run"
            )


def _runner_owned_roots(
    plan_dir: Path,
    state_dir_arg: str | None,
    *,
    run_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Trees ASL owns, which never count as a repository the run reaches.

    An operator may legitimately keep the plan directory, the state root, or a
    run directory inside an unrelated checkout. Those are the runner's own
    bookkeeping, not the work whose destination this gate decides.
    """
    roots = [plan_dir, state.state_root(state_dir_arg)]
    if run_dir is not None:
        roots.append(run_dir)
    return tuple(roots)


def _repository_intent_message(
    report: Mapping[str, Any],
    *,
    subject: str = "run",
    registration: repository_registration.RegistrationResult | None = None,
) -> str:
    """Name the conflicting roots. ``report`` is already credential-redacted.

    ``registration`` is the outcome of the migration the run gate applies
    before pausing; without it the message asks for the whole repair by hand.
    """
    git_roots = ", ".join(report.get("git_roots", ()))
    head = (
        f"{subject} reaches {report.get('git_root_count', 0)} git repositories "
        f"({git_roots}) but manifest.md declares no top-level `## Repositories` "
        "table"
    )
    if registration is not None and registration.status == "applied":
        commands = "; then: ".join(registration.next_commands)
        return (
            f"{head}; the registry was derived from disk and written into the "
            f"plan ({len(registration.files)} file(s) rewritten), so nothing is "
            f"left to hand-edit; run: {commands}"
        )
    if registration is not None and registration.status == "refused":
        return (
            f"{head}; the registry could not be derived automatically "
            f"({registration.missing_fact}: {registration.detail}); supply "
            "that fact, then declare the registry, give every session read and "
            "write repository scopes, and prepare the workspace before running "
            "this plan"
        )
    return (
        f"{head}; declare the registry, give every session read and write "
        "repository scopes, and prepare the workspace before running this plan"
    )


def _run_repository_intent_report(
    plan_dir: Path,
    context: verification_models.VerificationContext,
    *,
    state_dir_arg: str | None,
) -> dict[str, Any] | None:
    """Judge the repositories this run intends to reach, or ``None`` if one.

    This is the final authority: preflight can be skipped by a loop-ready
    short circuit and prepare can be skipped entirely by a legacy plan, so a
    run that never re-derives the verdict here has no gate at all. A registered
    workspace is exempt because its registry already names every repository.
    """
    if context.workspace is not None:
        return None
    _, report = preflight_footprint.plan_repository_intent(
        plan_dir,
        context.root,
        extra_exempt_roots=_runner_owned_roots(plan_dir, state_dir_arg),
    )
    return report


def _apply_repository_registry(
    snapshot: plans_models.PlanSnapshot,
    *,
    cwd: Path,
) -> tuple[repository_registration.RegistrationResult | None, str | None]:
    """Write the registry this gate refuses for, when that is still safe.

    Safety is the migration's own rule rather than a private one repeated
    here: a registry makes a completed handoff's unqualified ``Files
    modified`` entries illegal, so
    :func:`repository_registration.register_repositories` refuses a plan
    carrying one before writing a byte. The run pauses either way, because the
    rebind the migration names re-runs the prepare pipeline — a validator
    harness and fresh baselines — which is too large a side effect to trigger
    silently from a gate.
    """
    try:
        result = repository_registration.register_repositories(
            snapshot.plan_dir, cwd=cwd
        )
    except (plans_errors.PlanError, OSError) as error:
        return None, str(error)
    return result, None


def _task_packet_artifact_paths(
    plan_dir: Path,
    session_id: str,
    predecessor_id: str | None,
) -> tuple[Path, ...]:
    """Every artifact a task packet tells the worker to open and act on."""
    selected = plans_session_ids.parse_session_id(session_id)
    paths = [
        plan_dir / selected.filename,
        plan_dir / selected.expansion_filename,
        plan_dir / "context.md",
        plan_dir / plans_progress.PROGRESS_FILE_NAME,
    ]
    if predecessor_id is not None:
        paths.append(
            plan_dir / plans_session_ids.parse_session_id(predecessor_id).filename
        )
    return tuple(paths)


_PLAN_STORE_WRITES_RE = re.compile(
    r"\b(?:cross[- ]plan[- ]store|plan[- ]store)\s+writes?\b", re.IGNORECASE
)
_STATE_ROOT_ARGUMENT_RE = re.compile(
    r"--state-dir(?:\s*=\s*|\s+)(?:`([^`\n]+)`|\"([^\"\n]+)\"|'([^'\n]+)'|([^\s`,;]+))"
)


def _session_plan_store_state_root_report(
    plan_dir: Path,
    session_id: str,
    *,
    state_dir_arg: str | None,
    run_dir: Path,
) -> dict[str, Any] | None:
    """Refuse an explicit plan-store write requirement the guard cannot honor.

    Only the selected session's Done-when bullets declare this requirement.
    Flags quoted in examples, verification commands or historical handoffs
    are not configuration demands. This is deliberately not a prose write
    detector: a named external path can legitimately be read-only.

    The runner never supplies the missing override itself. Covering a broader
    state tree is an operator decision, and changing it also moves the run's
    own bookkeeping.
    """
    try:
        contract = contracts.read_completion_contract(plan_dir, session_id)
    except plans_errors.PlanError:
        # Missing/malformed contracts keep their existing verification gate;
        # this check must not turn a contract-repair turn into a config error.
        return None
    if contract is None:
        return None
    guard_roots = _runner_owned_roots(plan_dir, state_dir_arg, run_dir=run_dir)
    missing: set[str] = set()
    for requirement in contract.done_when:
        if _PLAN_STORE_WRITES_RE.search(requirement) is None:
            continue
        for match in _STATE_ROOT_ARGUMENT_RE.finditer(requirement):
            value = next(group for group in match.groups() if group is not None)
            # Never interpret shell variables or resolve a relative example
            # against ASL's own cwd. Only concrete filesystem roots bind this
            # check, including the operator's home-relative spelling.
            if not (value.startswith("/") or value.startswith("~/")):
                continue
            required_root = plans_files.canonicalize_path(value)
            if not any(
                workspaces.is_descendant(required_root, root, allow_equal=True)
                for root in guard_roots
            ):
                missing.add(str(required_root))
    if not missing:
        return None
    return {
        "session_id": session_id,
        "required_state_roots": sorted(missing),
        "effective_state_root": str(state.state_root(state_dir_arg)),
        "guard_roots": [str(root) for root in guard_roots],
        "source": (
            f"{plans_session_ids.parse_session_id(session_id).filename}"
            ":Completion Contract/Done when"
        ),
    }


def _task_packet_repository_intent_report(
    prompt: str,
    *,
    root: Path,
    artifact_paths: Sequence[Path],
    exempt_roots: Sequence[Path],
    provenance_roots: Sequence[Path] = (),
) -> dict[str, Any] | None:
    """Re-derive the verdict from what this turn actually hands the worker.

    The run-level gate judges the plan as split. A turn can carry more than
    that: expansion notes, shared context, a predecessor handoff, live mailbox
    guidance, an operator reply, and repair references all reach the worker
    through the rendered prompt or through the files it points at. Reading
    them here is what stops a location that no plan file declared from
    redirecting a legacy turn into a second repository.

    The artifacts read here are plan documents, so provenance metadata is
    filtered exactly as the plan-level scan filters it, and
    ``provenance_roots`` carries the declared source document forward. A turn
    quoting where the plan came from is not a turn that redirects anywhere.
    """
    candidates = list(repository_intent.candidates_from_plan_document(prompt))
    candidates.extend(repository_intent.candidates_from_task_packet(artifact_paths))
    for path in artifact_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        candidates.extend(repository_intent.candidates_from_plan_document(text))
    candidates.extend(repository_intent.candidates_from_execution_roots([root]))
    intent = repository_intent.resolve_intent(
        candidates,
        root=root,
        exempt_roots=exempt_roots,
        provenance_roots=provenance_roots,
    )
    if not intent.registry_required:
        return None
    return repository_intent.intent_report(intent)


def _ensure_legacy_run_metadata(
    plan_dir: Path,
    context: verification_models.VerificationContext,
) -> None:
    """First-bind an unprepared legacy plan without rewriting existing metadata."""
    if context.workspace is not None:
        return
    if plans_metadata.load_plan_metadata(plan_dir) is None:
        plans_metadata.save_plan_metadata(plan_dir, context.root)


def _max_review_fix_cycles(args: argparse.Namespace) -> int:
    """Resolve the repair/rereview cycle cap for this run.

    ``--max-review-fix-cycles`` wins; the deprecated ``--max-review-fix-plans``
    alias is honored when it is the only flag supplied; otherwise the default
    applies. Values are validated by :func:`_validate_run_inputs`.
    """
    cycles = getattr(args, "max_review_fix_cycles", None)
    if cycles is not None:
        return cycles
    legacy_cycles = getattr(args, "max_review_fix_plans", None)
    if legacy_cycles is not None:
        return legacy_cycles
    return DEFAULT_MAX_REVIEW_FIX_CYCLES


def _max_repair_group_attempts(args: argparse.Namespace) -> int:
    """Resolve the per-group repair reopen cap for this run."""
    attempts = getattr(args, "max_repair_group_attempts", None)
    if attempts is not None:
        return attempts
    return DEFAULT_MAX_REPAIR_GROUP_ATTEMPTS


def _max_review_fix_cycles_ceiling(args: argparse.Namespace) -> int:
    """Resolve the absolute repair/rereview cycle ceiling for this run."""
    ceiling = getattr(args, "max_review_fix_cycles_ceiling", None)
    if ceiling is not None:
        return ceiling
    return DEFAULT_MAX_REVIEW_FIX_CYCLES_CEILING
