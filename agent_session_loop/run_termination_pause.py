# ruff: noqa: F401, F403, F405, I001
"""Pause payload assembly, remediation, and durable pause records."""

from __future__ import annotations

# ruff: noqa: F401, F403, F405
from .run_termination_shared import *

#: Where a pause's remediation hints sit inside its details payload. A
#: readiness pause carries the gate payload as its details; the workspace-scope
#: pause nests that same payload under its own key.
_PAUSE_REMEDIATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("repair_hints",),
    ("readiness_gate", "repair_hints"),
)


def _optional_str(value: Any) -> str | None:
    """``value`` when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def _registered_repository_root(plan_dir: str | None, alias: str) -> str | None:
    """The root a registered alias resolves to, when the plan registers it."""
    if not plan_dir:
        return None
    try:
        registry = plans_registry.read_repository_registry(plan_dir)
        metadata = plans_metadata.load_plan_metadata(plan_dir)
    except plans_errors.PlanError, OSError, ValueError:
        return None
    if registry is None or not isinstance(
        metadata, plans_metadata.WorkspacePlanMetadata
    ):
        return None
    for repository in registry.repositories:
        if repository.alias == alias:
            return str(
                plans_files.canonicalize_path(
                    Path(metadata.workspace_root) / repository.path
                )
            )
    return None


def _commit_failed_remediation_message(
    details: Mapping[str, Any], *, plan_dir: str | None, cwd: str | None
) -> str | None:
    """Name the group, session and repository a ``commit-failed`` pause owes.

    The static hint says "re-run the failing git command in <repo-root>", and
    substituting the run's cwd there points a registered workspace at the
    workspace root rather than the repository whose commit failed. It also
    promised the run "resumes from the same session", when what a restart
    actually does first is retry the owed group (ASI-160). Returns ``None``
    when the pause carries no group, so the generic hint stands.
    """
    group_id = _optional_str(details.get("group_id"))
    session_id = details.get("session_id")
    if group_id is None:
        return None
    alias = _optional_str(details.get("failed_alias"))
    root = (
        _registered_repository_root(plan_dir, alias) if alias is not None else None
    ) or cwd
    where = f"`{root}`" if root else "the failing repository"
    subject = f"repository `{alias}`" if alias else "its repository"
    session = f" for session {session_id}" if session_id is not None else ""
    parts = [
        f"Commit group {group_id}{session} did not land in {subject}. ",
        f"Re-run the failing `git` command in {where} to see the error in full",
    ]
    stderr_path = _optional_str(details.get("stderr_path"))
    if stderr_path:
        parts.append(f" (the failed attempt's stderr is at `{stderr_path}`)")
    parts.append(
        ": a hook that rejected the commit, an unresolved merge/rebase state, "
        "or an index lock left by another process. Clear that condition and "
        f"re-run; the next run first retries outstanding commit group "
        f"{group_id}{session} before it selects any other session."
    )
    return "".join(parts)


def _runtime_reason_remediation(
    reason: str,
    *,
    plan_dir: str | None,
    cwd: str | None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The repair hint a stable pause reason resolves to, if it has one.

    Shaped like ``readiness_gate._readiness_repair_hints`` entries so a consumer
    reads one remediation vocabulary whichever source produced it. The plan
    directory and repository root are substituted when the payload knows them,
    leaving the placeholders intact rather than quoting them when it does not.
    A ``commit-failed`` pause that names its owed group is worded from those
    details instead of the generic hint.
    """

    hint = preflight_runtime_hints.runtime_repair_hint(reason)
    if hint is None:
        return None
    message = hint.message
    if plan_dir:
        message = preflight_auto_repair.repair_hint_payload(
            hint, plan_dir=plan_dir, cwd=cwd
        )["message"]
    if hint.category == "commit-failed":
        message = (
            _commit_failed_remediation_message(
                details or {}, plan_dir=plan_dir, cwd=cwd
            )
            or message
        )
    if (
        hint.category == "repository-registry-required"
        and (details or {}).get("repository_intent_source") == "task-packet"
    ):
        message = (
            "The rendered task packet names an additional Git repository. "
            "Inspect details.repository_intent for the paths; these may come "
            "from a reply, guidance, or a handoff rather than the plan. "
            "register-repositories scans plan files, so it can report "
            "not-required for this refusal. If the extra repository is intended, "
            "first declare its concrete paths and intended read/write scope in "
            "the session plan, then run "
            f"`asl register-repositories {shlex.quote(plan_dir or 'PLAN_DIR')} "
            f"--cwd {shlex.quote(cwd or 'REPO_ROOT')}` and the prepare/rebind "
            "command it prints. If the path was incidental, remove it from "
            "the continuation input that supplied it before retrying. "
            "A path mention alone does not grant access to that repository."
        )
    if hint.category == "commit-preflight":
        phase = (details or {}).get("commit_preflight_phase")
        if phase in ("startup", "continuation"):
            message = message.replace(
                "blocked startup or continuation", f"blocked {phase}", 1
            )
        repositories = (details or {}).get("commit_repositories")
        if isinstance(repositories, list):
            commands = [
                "`"
                + shlex.join(["git", "-C", row["resolved_root"], "status", "--short"])
                + "`"
                for row in repositories
                if isinstance(row, Mapping)
                and isinstance(row.get("resolved_root"), str)
            ]
            if commands:
                message += (
                    " Inspect the commit repositories with " + ", ".join(commands) + "."
                )
    return {
        "check_id": hint.check_id,
        "category": hint.category,
        "fix_category": hint.fix_category,
        "message": message,
    }


def _pause_remediation(
    details: Mapping[str, Any],
    *,
    reason: str,
    plan_dir: str | None = None,
    cwd: str | None = None,
    state_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Remediation for a pause: its own details first, else its reason.

    A gate that already resolved its findings to repair hints carries them in
    the pause details, and those are the most specific guidance available. A
    runner-side refusal carries findings but no hints, so the stable pause
    reason resolves one from
    ``preflight_runtime_hints.RUNTIME_REPAIR_HINTS`` instead. Without
    that fallback an ``inspect`` pause tells an operator to look without saying
    where, which is what the audit filed against `commit-preflight`.
    """

    for path in _PAUSE_REMEDIATION_PATHS:
        current: Any = details
        for key in path:
            current = current.get(key) if isinstance(current, Mapping) else None
        if isinstance(current, list) and current:
            return [entry for entry in current if isinstance(entry, Mapping)]
    resolution = details.get("mailbox_resolution")
    if (
        isinstance(resolution, Mapping)
        and resolution.get("action") == "pause"
        and pause_policy.classify_pause(reason)["user_action"]
        == pause_policy.USER_ACTION_REPLY
    ):
        command = ["asl", "run", plan_dir or "PLAN_DIR", "--resume", "--reply-stdin"]
        if state_dir:
            command.extend(["--state-dir", state_dir])
        return [
            {
                "check_id": "runtime.mailbox-pause-resolution",
                "category": reason,
                "fix_category": "invocation-fix",
                "message": (
                    "The pause resolution closed the mailbox question; it cannot "
                    "receive another inbox reply. When ready to supply a new "
                    f"decision, run `{shlex.join(command)}` with the answer on "
                    "stdin. This explicitly resumes the paused session with the "
                    "saved run configuration; the session stays paused until then."
                ),
            }
        ]
    fallback = _runtime_reason_remediation(
        reason, plan_dir=plan_dir, cwd=cwd, details=details
    )
    return [] if fallback is None else [fallback]


def _supersede_live_action_signal(
    payload: dict[str, Any], *, reason: str, question_closed: bool = False
) -> None:
    """Retire a live harness ask the pause being written has moved past.

    ``action_required`` is the run's current unresolved intervention, and a
    live signal — ``runner-pause``, ``runner-status-paused``,
    ``approval-required`` — is only current while the run is still stopped on
    it. A runner-declared refusal is not that: ``blocked``, ``unsafe`` and
    ``malformed-contract`` are the runner's own verdict, strictly more specific
    than the harness's generic ask, and the run stops on them, so the generic
    ask can no longer be answered in this run.

    Nothing else retires it. ``turn_execution_signals.clear_live_action_required``
    fires only when another turn starts or the run finishes unpaused, and a
    paused run reaches neither, so the stale signal outlived every pause that
    superseded it and reached the audit as a run still asking for something it
    had already moved past. The signal names the same session as the pause, so
    a same-session comparison would not have caught it either.

    The displaced record is kept rather than dropped, under the key the audit
    already uses for the retirement it performs itself.
    """
    signal = payload.get("action_required")
    if not isinstance(signal, Mapping):
        return
    if signal.get("reason") not in events.LIVE_ACTION_REQUIRED_REASONS:
        return
    if reason in events.LIVE_ACTION_REQUIRED_REASONS and not question_closed:
        # The pause is the ask. Retiring here would drop the more specific
        # record the turn raised in favour of the one assembled below.
        return
    payload["action_required"] = None
    payload[events.ACTION_REQUIRED_SUPERSEDED_KEY] = {
        "superseded_by": events.SUPERSEDED_BY_RUNNER_PAUSE,
        "pause_reason": reason,
        "record": dict(signal),
    }


def set_pause(
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Write the whole pause block onto ``payload``. The single writer.

    This boundary's primary public entry, and public precisely because every
    refusal in the run reaches it: a pause assembled field by field at a call
    site would classify its own reason and pick its own remediation, which is
    how ``asl status``, the supervisor helper and ``asl questions`` would end up
    reading three different pause vocabularies. Callers that also need the
    durable ``run-paused`` event use :func:`_pause_run`, which is this write plus
    the event and the AgentMux notification.
    """
    payload["ok"] = False
    payload["paused"] = True
    payload["reason"] = reason
    payload["pause_reason"] = reason
    # When, not just why. The overseer's fleet inventory decides whether a pause
    # is inside the window it was invoked for, and it may not open the event log
    # to find out, so the single pause writer is also the single stamp site. A
    # later pause overwrites an earlier one because the record describes the
    # pause the run is *in*, not the first one it ever took.
    payload[overseer_discovery.PAUSED_AT_KEY] = clock.isoformat(clock.utc_now())
    payload["pause_message"] = message
    payload["pause_details"] = {} if details is None else details
    payload["details"] = payload["pause_details"]
    # Carry the single-source-of-truth pause classification so consumers
    # (the supervisor helper, `asl status`, `asl questions`) stop hardcoding
    # the recoverable/user-required reason sets.
    policy = pause_policy.classify_pause(reason, payload["pause_details"])
    payload["pause_policy"] = policy
    resolution = payload["pause_details"].get("mailbox_resolution")
    question_closed = (
        isinstance(resolution, Mapping) and resolution.get("action") == "pause"
    )
    _supersede_live_action_signal(
        payload, reason=reason, question_closed=question_closed
    )
    # A pause a machine cannot retry is by definition waiting on a human, so the
    # terminal state names that ask rather than leaving an operator to infer it
    # from the reason. A signal the turn already raised is the more specific
    # record of the same ask and is left standing -- unless this pause is the
    # runner declaring its own refusal, in which case the retirement above has
    # already displaced it. Remediation is whatever the pause evidence carries:
    # a reason with no command still gets a record.
    action_required = payload.get("action_required")
    if policy["requires_user"] and (
        not isinstance(action_required, Mapping)
        or action_required.get("source") == "pause"
    ):
        # This writer owns a prior pause projection too. Refresh it when the
        # pause gains closure evidence instead of preserving an obsolete ask.
        config = payload.get("run_config")
        payload["action_required"] = {
            "reason": reason,
            "message": message,
            "source": "pause",
            "user_action": policy["user_action"],
            "remediation": _pause_remediation(
                payload["pause_details"],
                reason=reason,
                plan_dir=_optional_str(payload.get("plan_dir")),
                cwd=_optional_str(payload.get("cwd")),
                state_dir=_optional_str(
                    config.get("state_dir_override")
                    if isinstance(config, Mapping)
                    else None
                ),
            ),
        }


def _pause_run(
    event_path: Path,
    payload: dict[str, Any],
    *,
    reason: str,
    message: str,
    details: dict[str, Any] | None = None,
    persistence: run_persistence._RunPersistence | None = None,
) -> None:
    persistence = run_persistence._run_persistence(persistence)
    with persistence.lock:
        set_pause(payload, reason=reason, message=message, details=details)
        persistence.append_event(
            event_path,
            events.RUN_PAUSED_EVENT,
            {
                "run_id": payload.get("run_id"),
                "run_dir": payload.get("run_dir"),
                "reason": reason,
                "message": message,
                "details": (
                    {}
                    if details is None
                    else run_persistence._bounded_verification_details(details)
                ),
            },
        )
    run_persistence._notify_agent_mux_loop_paused(payload, persistence)
