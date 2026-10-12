"""Status, lock, and pause payload builders for the asl autopilot tests."""

from __future__ import annotations

from asl_autopilot_test_support import autopilot


def _status_payload(pending, *, lock=None, ok=True, next_pending_id="1"):
    return {
        "ok": ok,
        "counts": {"pending": pending, "complete": 0},
        "next_pending_id": None if pending == 0 else next_pending_id,
        "lock": lock or {"present": False, "active": False, "stale": False},
        "sessions": [],
        "plan_dir": "/tmp/plan",
    }


def _review_resume(
    action,
    *,
    eligible,
    blocker=None,
    minimum_required_cap=1,
    source_disposition=None,
    artifact_error=None,
):
    return {
        "source_cycle": 1 if source_disposition is not None else None,
        "review_session_id": "2" if source_disposition is not None else None,
        "source_disposition": source_disposition,
        "action": action,
        "eligible": eligible,
        "blocker": blocker,
        "historical_materializations": max(0, minimum_required_cap - 1),
        "minimum_required_cap": minimum_required_cap,
        "selected_cap": max(0, minimum_required_cap - 1),
        "artifact_valid": blocker != "artifact-invalid",
        "artifact_error": artifact_error,
    }


def _paused_work_commit(result="committed", *, repair_exhausted=False):
    """The record the runner's paused-work settlement writes into the pause.

    Mirrored by `run_loop` under `pause_details["paused_work_commit"]`, so it
    is nested rather than flat. That nesting is load-bearing here: the record
    carries a `verification_repair_exhausted` field of its own, and the
    supervisor's repair gate reads the run's flag at the top of the details.
    """
    return {
        "guard": "paused-work-commit",
        "pause_reason": "verify-failed",
        "commit_mode": "per-session",
        "result": result,
        "verification_repair_attempt": 1,
        "verification_repair_max_attempts": 2,
        "verification_repair_exhausted": repair_exhausted,
        "commit_group": {"group_id": "g1", "scope": "session-paused"},
        "commit": None,
        "error": None,
        "residue": {"repositories": []},
    }


def _settled_verify_failed_pause(**record_kwargs):
    """A verify-failed pause whose retained work the run committed on its way out."""
    return {
        "ok": False,
        "paused": True,
        "reason": "verify-failed",
        "pause_reason": "verify-failed",
        "pause_message": "verification failed",
        "pause_policy": autopilot.classify_pause("verify-failed"),
        "pause_details": {
            "session": "1",
            "verification_repair_attempt": 1,
            "verification_repair_max_attempts": 2,
            "verification_repair_exhausted": False,
            "paused_work_commit": _paused_work_commit(**record_kwargs),
        },
    }


def _commit_preflight_pause():
    """The dead end an unsettled relaunch used to meet at its startup gate."""
    return {
        "ok": False,
        "paused": True,
        "reason": "commit-preflight",
        "pause_reason": "commit-preflight",
        "pause_message": "the workspace has uncommitted changes",
        "pause_policy": autopilot.classify_pause("commit-preflight"),
    }


LOCK_CREATED_AT = "2026-07-24T18:59:00+00:00"


RUN_STARTED_AT = "2026-07-24T19:00:00+00:00"


ATTACHED_RUN_ID = "run-attached"


OLDER_RUN_STARTED_AT = "2026-07-24T18:00:00+00:00"


def _active_lock(plan_dir="/tmp/plan", *, stale=False, created_at=LOCK_CREATED_AT):
    lock = {
        "present": True,
        "active": True,
        "stale": stale,
        "plan_dir": plan_dir,
        "reason": "stale" if stale else "active",
    }
    if created_at is not None:
        lock["created_at"] = created_at
    return lock


def _attach_status(idle, *, session_id="tidy-db-writes", number=3, total=8):
    """Attach-disposition status: active matching lock + a live running turn."""
    status = _status_payload(1, lock=_active_lock())
    status["runner_state"] = {
        "paused": False,
        "active_turn": {
            "session_id": session_id,
            "session_number": number,
            "total_sessions": total,
            "idle_seconds": idle,
            "elapsed_seconds": 120,
        },
    }
    return status


def _attach_waiting_status(*, pending_outbox=1, session_id="12"):
    """Attach-disposition status whose runner is in a bounded reply wait."""
    status = _status_payload(1, lock=_active_lock())
    waiting = {
        "question_id": "00000000-0000-0000-0000-000000000012",
        "session_id": session_id,
        "session_number": int(session_id),
        "turn_index": 2,
        "base_deadline": "2099-07-24T19:05:00+00:00",
        "hard_deadline": "2099-07-24T19:15:00+00:00",
    }
    status["runner_state"] = {
        "paused": False,
        "active_turn": None,
        "active_verification": None,
        "waiting_for_reply": waiting,
    }
    status["waiting_for_reply"] = waiting
    status["mailbox"] = {"open_questions": pending_outbox}
    return status


def _attached_run_status(pending, *, run_id="run-a", lock_created_at=LOCK_CREATED_AT):
    """Attach-disposition status naming the `run_id` that holds the plan."""
    status = _status_payload(pending, lock=_active_lock(created_at=lock_created_at))
    status["runner_state"] = {
        "present": True,
        "error": None,
        "run_id": run_id,
        "ok": True,
        "paused": False,
        "reason": "",
        "run_started_at": RUN_STARTED_AT,
        "run_finished_at": None,
        "active_turn": {
            "session_id": "tidy-db-writes",
            "idle_seconds": 4,
            "elapsed_seconds": 90,
        },
        "stale_unfinished": False,
    }
    return status


def _startup_attach_status(pending, *, lock_created_at=LOCK_CREATED_AT):
    """Attach poll inside the runner's lock-before-state startup window.

    The holder has taken the plan lock but has not persisted the `state.json`
    carrying its `run_id` yet, so `asl status` reports an active lock over an
    absent runner state.
    """
    status = _status_payload(pending, lock=_active_lock(created_at=lock_created_at))
    status["runner_state"] = {
        "present": False,
        "error": None,
        "active_turn": None,
        "active_verification": None,
    }
    return status


def _finished_run_status(
    pending,
    *,
    run_id="run-a",
    reason="stopped",
    ok=True,
    finished=True,
    paused=False,
    run_started_at=RUN_STARTED_AT,
    last_action=None,
):
    """Unlocked status whose persisted runner_state describes a finished run."""
    status = _status_payload(pending)
    status["runner_state"] = {
        "present": True,
        "error": None,
        "run_id": run_id,
        "ok": ok,
        "paused": paused,
        "reason": reason,
        "run_started_at": run_started_at,
        "run_finished_at": "2026-07-24T19:20:00+00:00" if finished else None,
        "active_turn": None,
        "stale_unfinished": not finished,
    }
    if last_action is not None:
        status["runner_state"]["control"] = _control_block(last_action)
    return status


def _control_block(last_action):
    """The `control` projection a runner persists after a run-control action."""
    return {
        "stop_after_current": False,
        "tail_enabled": False,
        "abort_turn_requested": False,
        "abort_grace_seconds": None,
        "abort_reason": None,
        "abort_deadline": None,
        "next_harness": None,
        "consumed": True,
        "last_action": last_action,
    }


def _leftover_state_attach_status(
    pending,
    *,
    run_id="run-old",
    run_started_at=OLDER_RUN_STARTED_AT,
    lock_created_at=LOCK_CREATED_AT,
):
    """Startup-window attach poll with the *previous* run's state still on disk.

    A new run holds the lock, but it has not overwritten `state.json` yet, so
    the visible `run_id` names the run before it.
    """
    status = _finished_run_status(
        pending, run_id=run_id, reason="stopped", run_started_at=run_started_at
    )
    status["lock"] = _active_lock(created_at=lock_created_at)
    return status


def _running_status(idle, *, session_id="tidy-db-writes", number=3, total=8):
    """Status payload whose live turn is running with the given idle seconds."""
    return {
        "ok": True,
        "runner_state": {
            "paused": False,
            "active_turn": {
                "session_id": session_id,
                "session_number": number,
                "total_sessions": total,
                "idle_seconds": idle,
                "elapsed_seconds": 120,
            },
        },
    }


def _turn_heartbeat_record(idle, *, session_id="tidy-db-writes", number=3, total=8):
    return {
        "name": "turn_heartbeat",
        "payload": {
            "session_id": session_id,
            "session_number": number,
            "total_sessions": total,
            "idle_seconds": idle,
            "elapsed_seconds": 120,
        },
    }


def _hang_block(
    reason=autopilot.HANG_CLASS_RESULT_RECEIVED, *, idle=162.0, process_tree=None
):
    """Runner-shaped ``hang_suspected`` block (session-3 sentinel contract)."""
    return {
        "reason": reason,
        "detected_at": "2026-07-24T19:02:00+00:00",
        "idle_seconds": idle,
        "harness_pid": 4242,
        "result_seen_at": (
            "2026-07-24T19:01:00+00:00"
            if reason == autopilot.HANG_CLASS_RESULT_RECEIVED
            else None
        ),
        "confirm_samples": 2,
        "cpu_delta_seconds": 0.0,
        "process_tree": process_tree,
        "last_checked_at": "2026-07-24T19:03:00+00:00",
    }


def _cpu_progress_block(progressing=True, *, delta=0.58, confirm_samples=0):
    """Runner-shaped ``hang_cpu_progress`` sample."""
    return {
        "harness_pid": 4242,
        "progressing": progressing,
        "cpu_delta_seconds": delta,
        "confirm_samples": confirm_samples,
        "last_checked_at": "2026-07-24T19:03:00+00:00",
    }


def _hung_running_snapshot(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Tailer-shaped running snapshot carrying a hang_suspected block."""
    return {
        "state": "running",
        "session_id": "tidy-db-writes",
        "session_number": 3,
        "total_sessions": 8,
        "idle_seconds": idle,
        "elapsed_seconds": 300,
        "hang_suspected": _hang_block(reason, idle=idle),
    }


def _hung_run_status(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Full ``asl status`` payload with paths + a hang-suspected live turn."""
    return {
        "ok": True,
        "plan_dir": "/tmp/plan",
        "state_path": "/tmp/plan/.asl/state.json",
        "events_path": "/tmp/plan/.asl/events.jsonl",
        "active_turn": {
            "session_id": "tidy-db-writes",
            "session_number": 3,
            "total_sessions": 8,
            "idle_seconds": idle,
            "elapsed_seconds": 300,
            "stdout_path": "/tmp/plan/.asl/turn-stdout.log",
            "stderr_path": "/tmp/plan/.asl/turn-stderr.log",
            "last_message_path": "/tmp/plan/.asl/last-message.txt",
            "result_path": "/tmp/plan/.asl/result.json",
            "hang_suspected": _hang_block(reason, idle=idle),
        },
    }


def _hung_attach_status(idle=162.0, *, reason=autopilot.HANG_CLASS_RESULT_RECEIVED):
    """Attach-disposition status whose live turn carries a hang block."""
    status = _attach_status(idle)
    status["runner_state"]["active_turn"]["hang_suspected"] = _hang_block(
        reason, idle=idle
    )
    return status
