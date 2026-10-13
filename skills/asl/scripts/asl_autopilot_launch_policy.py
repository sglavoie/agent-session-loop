"""Launch/relaunch decisions and resume configuration derived from ``asl status``."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence

from asl_autopilot_constants import (
    DEFAULT_MAX_REVIEW_FIX_CYCLES,
    SESSION_KIND_IMPLEMENTATION,
    SESSION_KIND_REVIEW,
)

from agent_session_loop import assurance_report as _assurance_report


def _current_session_id(status: dict) -> str | None:
    """Session the retry budget keys off: the plan's next pending session."""
    value = status.get("next_pending_id")
    return str(value) if value is not None else None


def _pending_count(status: dict) -> int:
    """Authoritative pending-session count from an ``asl status`` payload."""
    counts = status.get("counts") or {}
    value = counts.get("pending")
    if isinstance(value, int):
        return value
    sessions = status.get("sessions") or []
    return sum(1 for entry in sessions if entry.get("status") == "pending")


def _argv_with_max_sessions(argv: Sequence[str], value: int) -> list:
    """Return a copy of ``argv`` with the ``--max-sessions`` value set to ``value``.

    ``--max-sessions`` is a *global* budget, so each relaunch must be capped at
    the sessions still remaining rather than the original request; otherwise a
    retry after a pause that already completed some sessions could overshoot the
    budget. ``parse_command`` always emits ``--max-sessions`` when the operator
    passed one, but the insert branch keeps this correct even if that ever
    changes.
    """
    out = list(argv)
    for index, token in enumerate(out):
        if token == "--max-sessions" and index + 1 < len(out):
            out[index + 1] = str(value)
            return out
    insert_at = out.index("--json") if "--json" in out else len(out)
    out[insert_at:insert_at] = ["--max-sessions", str(value)]
    return out


def _status_workspace(status: object) -> dict | None:
    """Return a complete ordered workspace projection from authoritative status."""
    if not isinstance(status, dict) or not status.get("ok"):
        return None
    runner_state = status.get("runner_state")
    candidates = [status]
    if isinstance(runner_state, dict):
        candidates.insert(0, runner_state)
    for payload in candidates:
        root = payload.get("workspace_root")
        repositories = payload.get("repositories")
        if not isinstance(root, str) or not root:
            continue
        if not isinstance(repositories, list) or any(
            not isinstance(repository, dict)
            or not isinstance(repository.get("alias"), str)
            for repository in repositories
        ):
            continue
        return {
            "workspace_root": root,
            "registry_digest": payload.get("registry_digest"),
            "repositories": [dict(repository) for repository in repositories],
        }
    return None


def _workspace_launch_error(parsed: dict, status: dict) -> str | None:
    workspace = _status_workspace(status)
    requested = parsed.get("cwd")
    if workspace is None or not requested:
        return None
    requested_root = os.path.realpath(os.path.abspath(os.path.expanduser(requested)))
    bound_root = os.path.realpath(
        os.path.abspath(os.path.expanduser(workspace["workspace_root"]))
    )
    if requested_root == bound_root:
        return None
    return (
        "--cwd does not match the prepared workspace binding; autopilot will "
        "not rebind automatically. Use `asl prepare PLAN_DIR --resume --cwd "
        "NEW_WORKSPACE --rebind-workspace`, then relaunch."
    )


def _parsed_with_status_workspace(parsed: dict, status: dict) -> dict:
    """Add the prepared workspace root to a launch that omitted ``--cwd``."""
    workspace = _status_workspace(status)
    if workspace is None or parsed.get("cwd"):
        return parsed
    root = workspace["workspace_root"]
    argv = list(parsed["argv"])
    insert_at = argv.index("--json") if "--json" in argv else len(argv)
    argv[insert_at:insert_at] = ["--cwd", root]
    run_args = list(parsed.get("run_args") or argv[1:])
    run_insert = run_args.index("--json") if "--json" in run_args else len(run_args)
    run_args[run_insert:run_insert] = ["--cwd", root]
    return {
        **parsed,
        "cwd": root,
        "workspace_root": root,
        "registry_digest": workspace.get("registry_digest"),
        "repositories": workspace.get("repositories", []),
        "argv": argv,
        "run_args": run_args,
    }


def _review_resume_payload(payload: dict | None) -> dict | None:
    """Return a payload's additive ``review_resume`` projection, if present."""
    if not isinstance(payload, dict):
        return None
    review_resume = payload.get("review_resume")
    return review_resume if isinstance(review_resume, dict) else None


def _assurance_payload(payload: dict | None) -> dict | None:
    """Return a bounded, display-only projection of a payload's assurance object.

    Built through ``agent_session_loop.assurance_report`` so the supervision
    result, ``asl run``, ``asl status``, and the Workbench all describe one
    audit the same way. The projection carries a verdict and the inbox command;
    it carries nothing with which this helper could configure, launch, retry, or
    dispose of an audit, and this helper never writes to the ledger.
    """
    if not isinstance(payload, dict):
        return None
    assurance = payload.get("assurance")
    if not isinstance(assurance, dict):
        runner_state = payload.get("runner_state")
        assurance = (
            runner_state.get("assurance") if isinstance(runner_state, dict) else None
        )
    bounded = _assurance_report.bounded_summary(assurance)
    return bounded or None


def _resume_run_config(parsed: dict, status: dict) -> dict | None:
    """Load the persisted ``run_config`` for a ``--resume`` supervised command.

    A ``--resume`` command reruns the plan against the flags its last run
    persisted, so the effective review harness and repair cap live in the
    plan's ``state.json`` (``run_config`` block), not in the resume-only command
    string. ``asl status --json`` advertises the ``state_path`` of that file, so
    this reads it the same way the runner's ``_load_resume_state`` does. Returns
    ``None`` — so the caller falls back to the parsed command's own values —
    when the command is not a resume, the status carries no readable
    ``state_path``, or the file lacks a ``run_config`` block.
    """
    if not parsed.get("resume") or not isinstance(status, dict):
        return None
    state_path = status.get("state_path")
    if not isinstance(state_path, str) or not state_path:
        return None
    try:
        with open(state_path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError, json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    run_config = payload.get("run_config")
    return run_config if isinstance(run_config, dict) else None


def _effective_review_config(parsed: dict, status: dict) -> tuple[str | None, object]:
    """Return the ``(review_harness, max_review_fix_cycles)`` governing a launch.

    For a normal command these come straight from the parsed command. A
    ``--resume`` command may omit ``--review-harness``/``--max-review-fix-cycles``
    and expect the runner to rehydrate them from the persisted ``run_config``;
    this resolves the same persisted values so the supervisor's all-complete
    review-continuation decision matches what the resumed child would actually
    do (rather than stopping ``complete`` because the resume-only command has no
    review harness and only the default cap). Explicitly-passed flags always
    win, mirroring the runner's resume overlay.
    """
    review_harness = parsed.get("review_harness")
    cap = parsed.get("max_review_fix_cycles", DEFAULT_MAX_REVIEW_FIX_CYCLES)
    run_config = _resume_run_config(parsed, status)
    if run_config is None:
        return review_harness, cap
    if review_harness is None:
        persisted_harness = run_config.get("review_harness")
        if persisted_harness:
            review_harness = persisted_harness
    if not parsed.get("max_review_fix_cycles_explicit"):
        persisted_cap = run_config.get("max_review_fix_cycles")
        if type(persisted_cap) is int and persisted_cap >= 0:
            cap = persisted_cap
    return review_harness, cap


def _should_launch_for_status(parsed: dict, status: dict, pending: int) -> bool:
    """Decide whether an unlocked status needs another ``asl run`` chunk.

    Pending sessions retain the historical launch behavior.  An all-complete
    plan only relaunches when a review harness is in effect and the additive
    ``review_resume`` projection explicitly asks for an initial review, repair
    materialization, or rereview.  The effective review harness and repair cap
    are resolved through :func:`_effective_review_config`, so a ``--resume``
    command honors the persisted ``run_config`` rather than the resume-only
    command's absent harness and default cap.  Repair materialization is gated
    against that effective absolute cap, not the cap persisted by a previous
    run.  Rereview consumes no materialization budget, so an eligible rereview
    launches regardless of that cap.  Missing projections preserve compatibility
    with older ``asl status`` payloads by keeping the old all-complete stop
    behavior.
    """
    if pending > 0:
        return True
    review_harness, effective_cap = _effective_review_config(parsed, status)
    if not review_harness:
        return False

    review_resume = _review_resume_payload(status)
    if review_resume is None:
        return False

    action = review_resume.get("action")
    if action == "initial-review":
        return True
    if action == "rereview":
        return review_resume.get("eligible") is True
    if action != "materialize-repairs":
        return False

    minimum_cap = review_resume.get("minimum_required_cap")
    if (
        type(minimum_cap) is not int
        or minimum_cap < 0
        or type(effective_cap) is not int
        or effective_cap < minimum_cap
    ):
        return False

    if review_resume.get("eligible") is True:
        return True
    # Read-only status evaluates against the most recently recorded cap.  A
    # newly raised command cap satisfies that one blocker prospectively.
    return review_resume.get("blocker") == "higher-cap-required"


def _expected_turn_harness(parsed: dict, active_turn: dict) -> str | None:
    """Which requested harness should be driving ``active_turn``, if knowable.

    A dual-harness run alternates between `--harness` (implementation) and
    `--review-harness` (review/rereview), so the flag a live turn must match
    depends on its `session_kind`. `None` means "no yardstick": the caller must
    not treat an observed harness as foreign.

    An absent kind is read as implementation, mirroring the runner's own
    default, so mismatch detection still works against older payloads. An
    unrecognized kind yields `None` — the helper cannot know which flag governs
    a leg it does not model, and guessing is what produced the false-mismatch
    bug. A review turn on a run with no `--review-harness` also yields `None`:
    plan and state identity already matched, so the lock is ours, and
    `parsed["harness"]` is the wrong yardstick for a review turn by
    construction.
    """
    kind = active_turn.get("session_kind")
    if kind == SESSION_KIND_REVIEW:
        return parsed.get("review_harness")
    if kind in (SESSION_KIND_IMPLEMENTATION, None):
        return parsed.get("harness")
    return None
