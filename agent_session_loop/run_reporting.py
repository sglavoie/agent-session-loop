"""Human-facing run progress and the public terminal-reporting surface.

Output serialization and inline tails live in ``run_reporting_output``;
read-only status projections live in ``run_reporting_status``. All reporting
shares one sink and lock, contains sink failures, and writes no durable state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .formatting import (
    format_duration_seconds,
    format_turn_finished_message,
    format_turn_started_message,
)
from .run_reporting_output import (
    INLINE_TAIL_INITIAL_LOG_LINES as INLINE_TAIL_INITIAL_LOG_LINES,
)
from .run_reporting_output import (
    INLINE_TAIL_JOIN_TIMEOUT_SECONDS as INLINE_TAIL_JOIN_TIMEOUT_SECONDS,
)
from .run_reporting_output import _CoordinatedStderrStream as _CoordinatedStderrStream
from .run_reporting_output import _InlineTailController as _InlineTailController
from .run_reporting_output import _local_timestamp as _local_timestamp
from .run_reporting_output import _StderrOutputCoordinator as _StderrOutputCoordinator
from .run_reporting_status import _compact_artifact_source as _compact_artifact_source
from .run_reporting_status import _compact_next_pending_id as _compact_next_pending_id
from .run_reporting_status import _compact_status_counts as _compact_status_counts
from .run_reporting_status import _compact_status_lines as _compact_status_lines
from .run_reporting_status import (
    _compact_total_duration_seconds as _compact_total_duration_seconds,
)
from .run_reporting_status import _compact_value as _compact_value
from .run_reporting_status import _format_local_datetime as _format_local_datetime
from .run_reporting_status import _format_session_label, _note_lines

#: How the progress reporter names a commit group. The run's labeller is
#: the commit lifecycle's to own, so it
#: enters at construction rather than being imported.
CommitGroupLabeler = Callable[[Mapping[str, Any]], str]

#: The payload key a run's terminalization writes its still-owed commit groups
#: under. The runner owns the constant every other reader uses; this is the
#: default :class:`_ProgressReporter` falls back to when a caller names none,
#: and ``tests/test_asl_run_reporting.py`` pins the two to the same string.
DEFAULT_UNRESOLVED_COMMIT_GROUPS_KEY = "unresolved_commit_groups"

#: What the relaunch finds, per outcome of the settlement a recoverable pause
#: makes for the work its turn retained. Keyed by the ``result`` field of the
#: record ``commit_settlement`` writes; an unrecognised result falls back to the
#: only thing that is always safe to say, which is that the changes are still
#: there. The consequence is the point of the sentence: a settlement exists so
#: the next process's startup commit gate meets a clean tree, and an operator
#: reading one of these lines is deciding whether to relaunch or to look.
PAUSED_WORK_DISPOSITIONS: dict[str, str] = {
    "committed": "the relaunch starts on a clean worktree",
    "no-changes": "the turn retained nothing to commit",
    "committed-with-residue": (
        "changes the commit did not take are still in the worktree and the "
        "relaunch will meet them at its startup commit gate"
    ),
    "failed": "the retained changes stay in the worktree for a human to inspect",
}


def default_commit_group_label(summary: Mapping[str, Any]) -> str:
    """Name one commit group the way the run's own summaries name it.

    The reporter needs a label before the commit lifecycle is a module of its
    own, so this is the fallback a caller that names no labeller gets. It reads
    only the three fields any commit-group summary carries; the equivalence
    against the runner's labeller is asserted rather than assumed.

    ``scope`` is read as a bare string rather than through the commit
    lifecycle's constant, because importing it here is the direction this
    boundary does not import in. The one value that changes the sentence is
    ``session-paused``: the retained-work commit a recoverable pause settles is
    a different group from the accepted commit for the same session, and a
    label that named only the session could not tell an operator which of the
    two a run lost.
    """
    session_id = summary.get("session_id")
    if session_id is None:
        scope = "end-of-run"
    elif summary.get("scope") == "session-paused":
        scope = f"session {session_id} retained work"
    else:
        scope = f"session {session_id}"
    alias = summary.get("failed_alias")
    return f"{scope} ({alias})" if alias else scope


def _progress_commit_label(commit: dict[str, Any]) -> str:
    scope = commit.get("scope")
    session = commit.get("session_id") or commit.get("session")
    if scope == "session" and session is not None:
        label = f"session {session}"
    elif scope == "end":
        label = "end"
    else:
        label = str(scope or "unknown")
    alias = commit.get("repository_alias") or commit.get("repo")
    return f"{label}, repository {alias}" if alias else label


class _ProgressReporter:
    """Human-facing progress output for non-JSON runs.

    Satisfies :class:`~agent_session_loop.turn_execution_protocols.TurnProgress`
    structurally, so the turn boundary reports through this object without
    either module importing the other. Every method here is a sentence and
    nothing more: the reporter decides how a fact reads, never whether the
    fact is true, and it is deliberately called outside every lock the run
    holds so a slow terminal can never stall a durable write.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        output: _StderrOutputCoordinator | None = None,
        commit_group_label: CommitGroupLabeler = default_commit_group_label,
        unresolved_groups_key: str = DEFAULT_UNRESOLVED_COMMIT_GROUPS_KEY,
    ) -> None:
        self.enabled = enabled
        self.output = (
            _StderrOutputCoordinator(enabled=enabled) if output is None else output
        )
        # The commit lifecycle's vocabulary enters here rather than being
        # imported: the run owns both the key its terminalization writes owed
        # groups under and the labeller that names one, and this module has no
        # business knowing either.
        self._commit_group_label = commit_group_label
        self._unresolved_groups_key = unresolved_groups_key

    def _emit(self, message: str) -> None:
        self.output.emit_line(message)

    def status_snapshot(self, lines: list[str]) -> None:
        self.output.emit_lines(lines)

    def run_started(self, payload: dict[str, Any]) -> None:
        snapshot = payload["initial_snapshot"]
        counts = snapshot["counts"]
        total = sum(counts.values())
        next_pending = snapshot["next_pending"]
        next_label = "none" if next_pending is None else f"session {next_pending}"
        self._emit(
            "run started: "
            f"harness {payload['harness']}; "
            f"run {payload['run_id']}; "
            f"{counts['complete']}/{total} sessions complete; "
            f"{counts['pending']} pending; "
            f"next {next_label}; "
            f"approval {payload['approval_mode']}; "
            f"run dir {payload['run_dir']}; "
            f"state {payload['state_path']}; "
            f"events {payload['events_path']}"
        )

    def orphaned_state(self, candidates: list[dict[str, Any]]) -> None:
        if not candidates:
            return
        count = len(candidates)
        director = "y" if count == 1 else "ies"
        self._emit(
            f"warning: {count} orphaned state director{director} from a moved "
            "plan match this plan's content"
        )
        for candidate in candidates:
            self._emit(
                f"  {candidate['state_dir']} "
                f"(old plan {candidate['old_plan_dir']}); "
                f"migrate with: {candidate['migrate_command']}"
            )

    def turn_started(self, active_turn: dict[str, Any]) -> None:
        self._emit(format_turn_started_message(active_turn))

    def heartbeat(self, heartbeat: dict[str, Any]) -> None:
        stream = heartbeat.get("last_activity_stream") or "none"
        self._emit(
            f"session {_format_session_label(heartbeat)} heartbeat "
            f"(elapsed {format_duration_seconds(heartbeat['elapsed_seconds'])}, "
            f"idle {format_duration_seconds(heartbeat['idle_seconds'])}, "
            f"last activity "
            f"{_format_local_datetime(heartbeat.get('last_activity_at'))}, "
            f"stream {stream})"
        )

    def action_required(self, signal: dict[str, Any]) -> None:
        self._emit(
            f"action required in session {_format_session_label(signal)} "
            f"(reason {signal['reason']}, "
            f"source {signal['source']}): "
            f"{signal['message']}"
        )

    def turn_finished(self, turn: dict[str, Any]) -> None:
        self._emit(format_turn_finished_message(turn))

    def verification_started(self, payload: dict[str, Any]) -> None:
        check_count = payload.get("check_count")
        plural = "" if check_count == 1 else "s"
        self._emit(
            f"verification started for session {_format_session_label(payload)} "
            f"turn {payload.get('turn_index')} ({check_count} check{plural})"
        )

    def verification_check_started(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} started: "
            f"{payload['name']} ({payload['kind']})"
        )

    def verification_heartbeat(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification heartbeat for session {_format_session_label(payload)} "
            f"check {payload['check_index'] + 1}/{payload['check_count']} "
            f"{payload['name']} "
            f"(elapsed {format_duration_seconds(payload['elapsed_seconds'])}, "
            f"check elapsed "
            f"{format_duration_seconds(payload['check_elapsed_seconds'])})"
        )

    def verification_check_finished(self, payload: dict[str, Any]) -> None:
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} finished: "
            f"{payload['name']} ({payload['kind']}) "
            f"status {payload['status']}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])}"
        )

    def verification_check_skipped(self, payload: dict[str, Any]) -> None:
        dependencies = ", ".join(payload.get("on_failure_of") or [])
        self._emit(
            f"verification check {payload['check_index'] + 1}/"
            f"{payload['check_count']} skipped: "
            f"{payload['name']} ({payload['kind']}), "
            f"condition {payload['reason']}, on failure of {dependencies}"
        )

    def verification_finished(self, payload: dict[str, Any]) -> None:
        outcome = "passed" if payload.get("passed") else "failed"
        skipped = int(payload.get("skipped_count", 0) or 0)
        skipped_suffix = f", {skipped} skipped" if skipped else ""
        gate_count = int(payload.get("gate_count", payload["check_count"]))
        gate_plural = "" if gate_count == 1 else "s"
        self._emit(
            f"verification {outcome} for session {_format_session_label(payload)} "
            f"({payload['passed_count']}/{payload['check_count']} checks passed, "
            f"{gate_count} gate{gate_plural}"
            f"{skipped_suffix}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])})"
        )

    def commit_started(self, commit: dict[str, Any]) -> None:
        self._emit(
            f"commit {commit['commit_index']} started "
            f"({_progress_commit_label(commit)})"
        )

    def commit_finished(self, commit: dict[str, Any]) -> None:
        sha = commit.get("commit_sha") or "none"
        self._emit(
            f"commit {commit['commit_index']} finished "
            f"({_progress_commit_label(commit)}, "
            f"status {commit['status_result']}, "
            f"sha {sha}, "
            f"duration {format_duration_seconds(commit.get('duration_seconds'))})"
        )

    def commit_failed(self, record: Mapping[str, Any]) -> None:
        """Report a commit failure the run absorbed rather than paused on."""
        detail = record.get("error") or record.get("message") or "unknown error"
        disposition = (
            "retrying this group at the next commit boundary"
            if record.get("retrying")
            else "leaving these changes uncommitted"
        )
        self._emit(
            f"commit failed: {self._commit_group_label(record)}: {detail}; "
            f"the run continues, {disposition}"
        )

    def commit_group_recovered(self, summary: Mapping[str, Any]) -> None:
        self._emit(f"commit group recovered: {self._commit_group_label(summary)}")

    def paused_work_committed(self, record: Mapping[str, Any]) -> None:
        """Report what became of the work a recoverable pause retained.

        The outcome word alone is not the operator's question. What they are
        about to do is relaunch, so every result carries the clause that says
        what the relaunch will find: a clean worktree, or the same changes
        still sitting in front of the next process's startup commit gate.

        A failed settlement names its cause the way :meth:`commit_failed`
        does, and reads the commit's own verdict when there is no exception to
        quote — a refused commit is not an error raised around the commit, so
        ``error`` is empty exactly when ``commit.status_result`` is the fact.
        The group is the only part of the record that knows which session this
        was, so the line names one only when a group was built.
        """
        result = str(record.get("result") or "unknown")
        reason = record.get("pause_reason") or "unknown"
        group = record.get("commit_group")
        scope = (
            f" for {self._commit_group_label(group)}"
            if isinstance(group, Mapping)
            else ""
        )
        commit = record.get("commit")
        detail = record.get("error") or (
            commit.get("status_result") if isinstance(commit, Mapping) else None
        )
        cause = f": {detail}" if detail else ""
        disposition = PAUSED_WORK_DISPOSITIONS.get(
            result, "leaving the retained changes in the worktree"
        )
        self._emit(
            f"paused work {result}{scope} after the {reason} pause{cause}; "
            f"{disposition}"
        )

    def run_finished(self, payload: dict[str, Any]) -> None:
        verb = "run finished" if payload["ok"] else "run paused"
        reason = payload.get("pause_reason") or payload.get("reason") or "unknown"
        for line in _note_lines(payload, prefix="note"):
            self._emit(line)
        unresolved = payload.get(self._unresolved_groups_key)
        if isinstance(unresolved, list) and unresolved:
            # A non-fatal warning, printed before the verdict line rather than
            # instead of it: the run really did finish, and it really did lose
            # these commits. Saying only one of the two would be a lie either
            # way, so the summary says both.
            labels = ", ".join(
                self._commit_group_label(summary)
                for summary in unresolved
                if isinstance(summary, Mapping)
            )
            self._emit(
                f"warning: {len(unresolved)} commit group(s) were never "
                f"completed: {labels}; their changes are still uncommitted"
            )
        self._emit(
            f"{verb}: "
            f"run {payload['run_id']}, "
            f"reason {reason}, "
            f"duration {format_duration_seconds(payload['duration_seconds'])}"
        )
