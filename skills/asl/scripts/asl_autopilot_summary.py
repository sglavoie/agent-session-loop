"""Human-readable summaries and operator guidance for autopilot results."""

from __future__ import annotations

from asl_autopilot_constants import STOP_KIND_ABORT
from asl_autopilot_launch_policy import _effective_review_config, _review_resume_payload

from agent_session_loop import review_continuation as _review_continuation


def _review_resume_guidance(result: dict) -> str | None:
    """Return concise operator guidance from a surfaced review projection.

    Thin wrapper over :func:`review_continuation.review_resume_guidance`, the
    single renderer shared with the runner and the CLI, so all three describe
    the same continuation identically. Review-harness activity is derived from
    :func:`_effective_review_config` — the same resolver that governs the
    launch decision and terminal verdict attachment — so a resume-only command
    whose harness lives in the persisted ``run_config`` is recognized as
    review-enabled and is not told to add an explicit review harness the runner
    already rehydrates.
    """
    command = result.get("command")
    parsed = command if isinstance(command, dict) else {}
    status = result.get("status")
    review_harness, _ = _effective_review_config(
        parsed, status if isinstance(status, dict) else {}
    )
    return _review_continuation.review_resume_guidance(
        _review_resume_payload(result),
        review_harness_active=bool(review_harness),
    )


def _hung_leaf_command(hang: dict) -> str | None:
    """Command string of the deepest descendant in the sampled process tree.

    ``process_tree`` is BFS-ordered (parents before children), so the last
    entry is the deepest descendant — the best hung-leaf candidate.
    """
    block = hang.get("hang_suspected")
    if not isinstance(block, dict):
        return None
    tree = block.get("process_tree")
    if not isinstance(tree, list) or not tree:
        return None
    leaf = tree[-1]
    if not isinstance(leaf, dict):
        return None
    command = leaf.get("command")
    if isinstance(command, str) and command:
        return command
    return None


def _assurance_guidance(result: dict) -> str | None:
    """Name what the supervisor audit found, or ``None`` when it found nothing.

    Silent on ``all-clear``, ``skipped``, and ``pending``: a one-line summary
    that announces a clean audit every time trains an operator to stop reading
    it, which is exactly when the one line that matters would be missed.
    """
    assurance = result.get("assurance")
    if not isinstance(assurance, dict) or not assurance.get("issue_ids"):
        return None
    listed = ", ".join(str(item) for item in assurance["issue_ids"])
    if assurance.get("issue_ids_truncated"):
        listed = f"{listed}, …"
    qualifier = (
        " (partial: the audit could not complete)" if assurance.get("partial") else ""
    )
    return (
        f"supervisor audit found {assurance.get('issue_count', 0)} issue(s)"
        f"{qualifier}: {listed}; run `{assurance.get('list_command')}`"
    )


def _human_summary(result: dict) -> str:
    """One-line human summary for non-JSON output, plus any audit verdict."""
    summary = _run_human_summary(result)
    audit = _assurance_guidance(result)
    return f"{summary}; {audit}" if audit else summary


def _run_human_summary(result: dict) -> str:
    """One-line human summary of how the supervised run itself ended."""
    reason = result.get("reason", "unknown")
    if reason == "worker_hung":
        hang = result.get("hang")
        hang = hang if isinstance(hang, dict) else {}
        hang_class = hang.get("class") or "unknown"
        idle_seconds = hang.get("idle_seconds")
        if isinstance(idle_seconds, (int, float)) and not isinstance(
            idle_seconds, bool
        ):
            idle_text = f"idle {int(idle_seconds)}s"
        else:
            idle_text = "idle unknown"
        summary = f"stopped: worker_hung - {hang_class} ({idle_text})"
        leaf = _hung_leaf_command(hang)
        if leaf:
            summary = f"{summary}; deepest descendant: {leaf}"
        return (
            f"{summary}; the `asl run` child is still alive — inspect before "
            "killing anything; re-run the same command to re-attach"
        )
    if reason == "review_findings":
        guidance = _review_resume_guidance(result)
        review = result.get("review") or {}
        status_line = review.get("status_line", "review reported findings")
        summary = f"stopped: review_findings - {status_line}"
        return f"{summary}; {guidance}" if guidance else summary
    if reason == "max-sessions":
        completed = result.get("completed")
        pending = result.get("pending")
        return (
            f"ok: max-sessions - completed {completed} session(s), "
            f"{pending} still pending; re-run the same command to continue"
        )
    if reason == "stopped":
        # A honored control request: report it exactly like the budget stop — a
        # clean early stop with progress counts, never a pause, a failure, or
        # `blocked`. An abort additionally reverted the session it ended, so the
        # line says so; a reader who sees only the counts would otherwise assume
        # that session landed.
        completed = result.get("completed")
        pending = result.get("pending")
        if result.get("stop_kind") == STOP_KIND_ABORT:
            return (
                f"ok: stopped - supervisor abort ended the in-flight turn and "
                f"returned its session to pending; completed {completed} "
                f"session(s), {pending} still pending; re-run the same command "
                "to continue"
            )
        return (
            f"ok: stopped - graceful stop after the current session; completed "
            f"{completed} session(s), {pending} still pending; re-run the same "
            "command to continue"
        )
    guidance = None
    if reason == "complete":
        guidance = _review_resume_guidance(result)
    elif reason == "blocked":
        review_resume = _review_resume_payload(result)
        if review_resume is not None and review_resume.get("action") == "terminal":
            guidance = _review_resume_guidance(result)
    detail = result.get("error") or result.get("detail")
    verdict = "ok" if result.get("ok") else "stopped"
    summary = f"{verdict}: {reason} - {detail}" if detail else f"{verdict}: {reason}"
    return f"{summary}; {guidance}" if guidance else summary
