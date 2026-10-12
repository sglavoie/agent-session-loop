"""Shared constants for the test_asl_control_* modules."""

from __future__ import annotations

from agent_session_loop import control_signal

# What a producer gets for a live runner that published a wakeup endpoint. No
# platform conditional survives: the endpoint is a filesystem object rather
# than a process handle, so every host that can host a FIFO delivers.
LIVE_WAKEUP = control_signal.NotifyResult(True, "delivered")


def live_active_turn() -> dict[str, object]:
    return {
        "run_id": "run-1",
        "turn_index": 1,
        "session": 1,
        "session_id": "1",
        "total_sessions": 1,
        "harness": "codex",
        "artifact_dir": "/tmp/turn",
        "stdout_path": "/tmp/stdout.log",
        "stderr_path": "/tmp/stderr.log",
        "last_message_path": "/tmp/last-message.txt",
        "result_path": "/tmp/result.json",
        "started_at": "2026-07-24T12:00:00+00:00",
        "last_activity_at": "2026-07-24T12:00:00+00:00",
        "heartbeat_count": 0,
    }
