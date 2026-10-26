"""Turn log, path and ``result.json`` payload construction for one turn."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    config,
    durability,
    events,
    harness_failures,
    harnesses,
    plans_models,
    plans_payloads,
    process_tree,
    turn_execution_argv,
    turn_execution_constants,
    turn_execution_live,
    turn_execution_signals,
)

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _log_text(text: str) -> str:
    """``text`` with any lone surrogate replaced, so it can be written as UTF-8.

    A JS harness that slices a string mid-emoji emits a lone ``\\ud83d``
    escape, which ``json.loads`` keeps and a strict UTF-8 write refuses; the
    pipe decoder already replaces undecodable bytes the same way.
    """
    return _LONE_SURROGATE.sub("\ufffd", text)


def prepare_stream_logs(paths: dict[str, Path]) -> harnesses.StreamCallback:
    def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
        durability.append_text(paths[stream_name], _log_text(text))

    return stream_callback


def create_turn_logs(paths: dict[str, Path]) -> None:
    """Materialize the turn's empty logs at the launch boundary.

    Created any earlier, a pause from one of the pre-launch guards leaves
    behind a turn directory holding nothing but two empty logs, which reads
    downstream as a turn that ran and wrote no `result.json`.
    """
    durability.write_text(paths["stdout"], "")
    durability.write_text(paths["stderr"], "")


def live_stream_callback(
    log_stream_callback: harnesses.StreamCallback,
    live_turn: turn_execution_live.LiveTurn,
) -> harnesses.StreamCallback:
    def stream_callback(stream_name: harnesses.StreamName, text: str) -> None:
        log_stream_callback(stream_name, text)
        live_turn.record_activity(stream_name, text)

    return stream_callback


def turn_paths(run_dir: Path, *, turn_index: int, session_id: str) -> dict[str, Path]:
    artifact_dir = (
        run_dir
        / f"turn-{turn_index:04d}-{plans_payloads.session_path_label(session_id)}"
    )
    return {
        "artifact_dir": artifact_dir,
        "stdout": artifact_dir / "stdout.log",
        "stderr": artifact_dir / "stderr.log",
        "last_message": artifact_dir / "last-message.txt",
        "result": artifact_dir / "result.json",
    }


def turn_status_result(
    *,
    result: harnesses.HarnessResult,
    status_changes: tuple[dict[str, Any], ...],
    pending_completed: tuple[str, ...],
    session_id: str,
    runner_signals: dict[str, Any],
    plan_error: str | None,
) -> str:
    if result.interrupted:
        return "interrupted"
    if result.timed_out:
        return "timeout"
    if result.conversation_restore_refused is not None:
        # The same name the run pauses under, so the turn record and the pause
        # tell one story. Classified from the exit code instead, this turn would
        # be filed as a harness that failed -- which is what it looks like and
        # not what happened: the provider would not restore the conversation,
        # and nothing ran.
        return events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
    if plan_error is not None:
        return "malformed-plan"
    runner_pause = runner_signals.get("runner_pause")
    if runner_pause is not None:
        return turn_execution_signals.runner_pause_reason(str(runner_pause))
    if result.returncode != 0:
        return harness_failures.classify_harness_failure(result).reason
    if not status_changes:
        return "no-status-change"
    if len(status_changes) > 1:
        return "multiple-status-changes"
    expected_change = plans_payloads.session_status_change_payload(
        session_id,
        before="pending",
        after="complete",
    )
    if status_changes != (expected_change,):
        return "unexpected-status-change"
    if pending_completed == (session_id,):
        return "complete"
    return "unknown"


def observed_model_payload(observed_model: str | None) -> dict[str, str | None]:
    """The model the harness reported executing, beside the requested one."""
    return {
        "observed_model": observed_model,
        "observed_model_source": (
            None if observed_model is None else config.MODEL_SOURCE_HARNESS_REPORTED
        ),
    }


_RESULT_PREVIEW = re.compile(r"\b[a-z]+ result: ")


def post_result_termination_note(
    result: harnesses.HarnessResult,
    *,
    last_activity_preview: object,
    harness_shutdown: object,
) -> str | None:
    """Say what ended a turn whose last output was its terminal result.

    ``None`` unless the last activity was a result line *and* something other
    than a clean exit followed it: a post-result shutdown record, an abort, an
    interrupt, a timeout or a nonzero exit. Those turns otherwise keep a
    preview reading "claude result: success" beside a returncode of 143.
    """
    if not isinstance(last_activity_preview, str) or not _RESULT_PREVIEW.search(
        last_activity_preview
    ):
        return None
    shutdown = harness_shutdown if isinstance(harness_shutdown, Mapping) else None
    recovery = (shutdown or {}).get("recovery")
    if result.aborted:
        cause = "aborted"
    elif result.interrupted:
        cause = "interrupted"
    elif result.timed_out:
        cause = "timed-out"
    elif (
        isinstance(recovery, Mapping)
        and recovery.get("policy") == process_tree.POST_RESULT_ROOT_RECOVERY_POLICY
    ):
        cause = "post-result-shutdown"
    elif shutdown is not None:
        cause = str(shutdown.get("result") or "harness-shutdown")
    elif result.returncode != 0:
        cause = "nonzero-exit"
    else:
        return None
    return f"harness terminated after result ({cause}, rc {result.returncode})"


def write_turn_artifacts(
    *,
    paths: dict[str, Path],
    result: harnesses.HarnessResult,
    before: plans_models.PlanSnapshot,
    after: plans_models.PlanSnapshot | None,
    run_id: str,
    turn_index: int,
    session_id: str,
    live_turn: dict[str, Any],
    plan_error: str | None = None,
    unredacted_prompt: str | None = None,
) -> dict[str, Any]:
    last_activity_preview = live_turn.get("last_activity_preview")
    termination_note = post_result_termination_note(
        result,
        last_activity_preview=last_activity_preview,
        harness_shutdown=live_turn.get("harness_shutdown"),
    )
    stdout_text = result.stdout
    if termination_note is not None:
        # The last line the harness wrote was its result, so the preview (and
        # the log's tail) would still read as a clean finish. Name what
        # actually ended the turn in both places (ASI-152).
        if stdout_text and not stdout_text.endswith("\n"):
            stdout_text += "\n"
        stdout_text += f"asl: {termination_note}\n"
    durability.write_text(paths["stdout"], _log_text(stdout_text))
    durability.write_text(paths["stderr"], _log_text(result.stderr))
    durability.write_text(paths["last_message"], _log_text(result.last_message))

    status_changes = (
        plans_payloads.status_changes(before, after) if after is not None else ()
    )
    pending_completed = (
        plans_payloads.pending_completed(before, after) if after is not None else ()
    )
    signals = turn_execution_signals.runner_signals(result)
    status_result = turn_status_result(
        result=result,
        status_changes=status_changes,
        pending_completed=pending_completed,
        session_id=session_id,
        runner_signals=signals,
        plan_error=plan_error,
    )
    persisted_argv = turn_execution_argv.persisted_result_argv(
        result.argv,
        live_turn=live_turn,
        unredacted_prompt=unredacted_prompt,
    )
    payload = {
        "version": turn_execution_constants.RESULT_SCHEMA_VERSION,
        "run_id": run_id,
        "turn_index": turn_index,
        **plans_payloads.session_identity_payload(session_id),
        "session_kind": live_turn.get(
            "session_kind", plans_payloads.SESSION_KIND_IMPLEMENTATION
        ),
        "total_sessions": live_turn.get("total_sessions"),
        "artifact_dir": str(paths["artifact_dir"]),
        "harness": result.name,
        "model": live_turn.get("model"),
        "model_source": live_turn.get("model_source"),
        **observed_model_payload(
            result.observed_model or live_turn.get("observed_model")
        ),
        "effort": live_turn.get("effort"),
        "effort_source": live_turn.get("effort_source"),
        "spark_route": live_turn.get("spark_route"),
        "argv": persisted_argv,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "stdout_path": str(paths["stdout"]),
        "stderr_path": str(paths["stderr"]),
        "last_message_path": str(paths["last_message"]),
        "result_path": str(paths["result"]),
        "status_changes": list(status_changes),
        "pending_completed": [
            plans_payloads.session_compat_value(completed_id)
            for completed_id in pending_completed
        ],
        "pending_completed_ids": list(pending_completed),
        "status_result": status_result,
        "started_at": live_turn["started_at"],
        "finished_at": live_turn["finished_at"],
        "duration_seconds": live_turn["duration_seconds"],
        "total_duration_seconds": live_turn.get("total_duration_seconds"),
        "idle_seconds_since_previous_turn": live_turn[
            "idle_seconds_since_previous_turn"
        ],
        "last_activity_at": live_turn.get("last_activity_at"),
        "last_activity_stream": live_turn.get("last_activity_stream"),
        "last_activity_preview": (
            last_activity_preview if termination_note is None else termination_note
        ),
        "last_heartbeat_at": live_turn.get("last_heartbeat_at"),
        "heartbeat_count": live_turn.get("heartbeat_count", 0),
        "before": plans_payloads.snapshot_payload(before),
        "after": plans_payloads.snapshot_payload(after) if after is not None else None,
    }
    if termination_note is not None:
        payload["last_harness_activity_preview"] = last_activity_preview
    if result.stream_errors:
        payload["stream_errors"] = list(result.stream_errors)
    if result.stream_io is not None:
        payload["stream_io"] = dict(result.stream_io)
    if (
        result.returncode != 0
        and not result.timed_out
        and signals.get("runner_pause") is None
    ):
        payload["failure_classification"] = harness_failures.classify_harness_failure(
            result, shutdown=live_turn.get("harness_shutdown")
        ).to_dict()
    degradation = harness_failures.classify_harness_degradation(result)
    if degradation is not None:
        payload["harness_degradation"] = degradation.to_dict()
    if live_turn.get("action_required") is not None:
        payload["action_required"] = live_turn["action_required"]
    if "harness_shutdown" in live_turn:
        payload["harness_shutdown"] = live_turn["harness_shutdown"]
    if plan_error is not None:
        payload["plan_error"] = plan_error
    prompt_kind = live_turn.get("prompt_kind")
    if prompt_kind is not None:
        payload["prompt_kind"] = prompt_kind
    reply_source = live_turn.get("reply_source")
    if reply_source is not None:
        payload["reply_source"] = reply_source
    resolution_source = live_turn.get("resolution_source")
    if resolution_source is not None:
        payload["resolution_source"] = resolution_source
    payload.update(signals)
    # Events are always fullsynced, so a non-durable result would let the
    # event stating this turn ended outlive the artifact it points at.
    durability.write_json_file(paths["result"], payload, durable=True)
    return payload
