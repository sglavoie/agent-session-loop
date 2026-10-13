"""Thresholds, environment names and vocabulary shared by turn execution."""

from __future__ import annotations

INTERRUPTED_EXIT = 130


HEARTBEAT_INTERVAL_SECONDS = 30.0


HANG_TRIGGER_IDLE_SECONDS = 120.0


HANG_CONFIRM_SAMPLES = 2


HANG_CPU_EPSILON_SECONDS = 0.5


HANG_RESULT_GRACE_SECONDS = 45.0


HANG_ROOT_SHUTDOWN_GRACE_SECONDS = 30.0


HANG_TRIGGER_IDLE_ENV_VAR = "ASL_HANG_TRIGGER_IDLE_SECONDS"


HANG_CONFIRM_SAMPLES_ENV_VAR = "ASL_HANG_CONFIRM_SAMPLES"


HANG_RESULT_GRACE_ENV_VAR = "ASL_HANG_RESULT_GRACE_SECONDS"


HANG_REASON_RESULT_RECEIVED = "result_received_process_alive"


HANG_REASON_ZERO_CPU = "zero_cpu_idle"


POST_RESULT_STDIN_RELEASE_POLICY = "post_result_stdin_release"


LIVE_CONTROL_POLL_INTERVAL_SECONDS = 5.0


# Streams whose control markers the runner is willing to act on. The agent's
# final message and stdout are what the agent chose to say; stderr is a
# transcript of everything that happened to it.
TRUSTED_RUNNER_SIGNAL_SOURCES: tuple[str, ...] = ("last_message", "stdout")


RUNNER_STATUS_SOURCE_ABSENT = "absent"


APPROVAL_SIGNAL_PATTERNS: tuple[str, ...] = (
    "approval required",
    "requires approval",
    "waiting for approval",
    "requesting approval",
    "needs approval",
    "approve this",
    "allow this command",
    "permission to proceed",
)


RESULT_SCHEMA_VERSION = 2
