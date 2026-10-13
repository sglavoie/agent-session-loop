"""Shared constants, defaults, and runner-module aliases for the autopilot."""

from __future__ import annotations

import re

from agent_session_loop import cli_trace as _cli_trace
from agent_session_loop import harnesses as _harnesses
from agent_session_loop import intervention as _intervention
from agent_session_loop import pause_policy as _pause_policy
from agent_session_loop import plans_payloads
from agent_session_loop import review as _review
from agent_session_loop import run_inputs as _run_inputs

HELPER_NAME = "asl-autopilot"
HELPER_DESCRIPTION = (
    "Supervise an `asl run` invocation end to end: parse the requested command, "
    "start or attach to the run, poll structured status, and surface pauses that "
    "need a human reply."
)

# Stage tag for the run supervision loop (start/attach/poll/chunk). Callers key
# off `stage` to distinguish a supervision-time result from a parse-time
# failure, and off `reason` for the specific verdict (`complete`,
# `max-sessions`, `stopped`, `user_required`, `paused`, `blocked`, `capped`,
# `status-error`, `run-error`). `max-sessions` and `stopped` are the two clean
# early stops: both are `ok: true` with `completed`/`pending` counts, never a
# pause or a failure.
RUN_STAGE = "run"

# `stopped` covers both out-of-band control requests, so the result carries a
# `stop_kind` naming which one ended the run. `abort` (`asl control abort`)
# killed the turn that was in flight and reverted its session to `pending`, so
# its work is *not* in `completed`; `stop-after-current` let the session finish
# and stopped at the boundary. Neither is an anomaly: both stay `ok: true` and
# are reported exactly like a spent `--max-sessions` budget.
STOP_KIND_ABORT = "abort"
STOP_KIND_AFTER_CURRENT = "stop-after-current"
# Terminal run-control actions an abort resolves to, as recorded in the run's
# `control.last_action`. Anything else beside `reason: "stopped"` is the
# session-boundary stop. `abort-turn-satisfied-by-worker` is listed for
# completeness only: a worker that ends its turn inside the grace window leaves
# its own pause or completion in charge, so that outcome normally surfaces as a
# pause or as a run that kept going rather than as `reason: "stopped"`.
# `finalization-abort-honored` is the abort that arrived after the last turn had
# already ended, cancelling the terminal audit instead of a turn.
ABORT_STOP_ACTIONS = (
    "abort-turn-honored",
    "abort-turn-satisfied-by-worker",
    "finalization-abort-honored",
)

# Stage tag for the safe command parser. Callers
# key off `stage` to distinguish a parse-time failure from a later start/poll
# failure, and off `reason` for the specific parse verdict.
PARSE_STAGE = "parse"

# Stage tag for the `--detach` launch handshake. The parent process reports only
# whether the supervising grandchild was launched; the supervision verdict
# itself lands in `result.json` under the launch's log directory.
LAUNCH_STAGE = "launch"

# Layout of a detached launch's log directory:
# `<plan state dir>/supervision/<supervision id>/`.
SUPERVISION_LOG_DIR_NAME = "supervision"
DETACH_RESULT_FILE_NAME = "result.json"
DETACH_STDERR_FILE_NAME = "stderr.log"

# Agent harnesses recognized by `asl run`, re-exported from the ASL package so
# the helper and the runner share one list by construction.
HARNESS_NAMES = _harnesses.HARNESS_NAMES

# Session kinds the runner stamps on `active_turn.session_kind`, re-exported for
# the same reason. A dual-harness run alternates between them: implementation
# legs run under `--harness`, review/rereview legs under `--review-harness`.
SESSION_KIND_IMPLEMENTATION = plans_payloads.SESSION_KIND_IMPLEMENTATION
SESSION_KIND_REVIEW = plans_payloads.SESSION_KIND_REVIEW

# Every `asl` child this helper spawns is internal automation, not a direct
# operator call, so its invocation trace is attributed to the helper.
INVOCATION_ORIGIN = _cli_trace.ORIGIN_AUTOPILOT

# Shell operators the helper refuses to interpret. The command is executed
# without a shell, so any of these would either be passed as a literal to the
# child (surprising) or, if a shell were ever involved, enable injection. We
# reject them outright rather than guess intent.
_SHELL_OPERATOR_CHARS = set("();<>|&")

# Leading `NAME=VALUE` env assignments (before the executable). Names follow the
# POSIX portable variable-name rule; anything starting with `-` is a flag, not an
# assignment, so it correctly falls through to the executable/args scan.
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# Supervision loop tuning. `POLL_INTERVAL` is how long the supervisor waits
# between `asl status` probes while attached to a run someone else is driving.
# `HEARTBEAT_INTERVAL` throttles the concise stderr progress lines (stdout stays
# reserved for the final JSON). `MAX_CHUNKS` bounds how many self-launched
# relaunches (pause retries and review continuations) we perform before giving
# up, so a stalled plan can never loop forever. (`--max-sessions` is a separate
# global session budget, not this safety bound.)
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_HEARTBEAT_INTERVAL = 30.0
DEFAULT_MAX_CHUNKS = 50

# Attach-mode backstop, measured in wall clock since the current attach episode
# began rather than in polls: one attach poll lasts 5s (`events.jsonl` grew) to
# 30s (the heartbeat interval elapsed), so a poll count drains ~6x faster on a
# busy run than a quiet one — fastest exactly when the run is healthiest. What
# polices progress here is the hang bars below; this only covers the residual
# case they cannot see, an active lock whose holder publishes no readable
# `runner_state`, leaving `_status_snapshot` at `{"state": "unknown"}` so
# neither bar can ever fire. Set far above `DEFAULT_HANG_IDLE_CEILING` so any
# run with readable telemetry escalates first, and above the wall-clock length
# of a real plan: capping early costs a supervision gap, where a worker question
# can time out into a terminal `blocked` pause with nobody watching. Overridable
# via kwargs only — deliberately not a CLI flag.
DEFAULT_ATTACH_MAX_SECONDS = 43200.0

# Self-launched children share their process wait with mailbox supervision:
# check for new questions and notices every five seconds and renew the
# supervisor lease on the same 30-second cadence as progress heartbeats.
# Message bodies stay in the structured outbox payload; stderr receives only a
# one-line escaped preview bounded to this many encoded characters.
MAILBOX_POLL_INTERVAL = 5.0
MAILBOX_LEASE_RENEW_INTERVAL = 30.0
QUESTION_PREVIEW_MAX_CHARS = 240

# Idle threshold (seconds) past which a heartbeat flags the child as possibly
# stalled. `active_turn.idle_seconds` is the seconds since the child last wrote
# stdout/stderr; the runner refreshes it on its own 30s internal heartbeat, so a
# healthy-but-slow child normally shows idle well under 90s. 90.0 is ≈3× that
# 30s cadence: high enough to avoid false positives from normal between-heartbeat
# staleness, low enough to surface a genuinely wedged child promptly. Overridable
# via the `stall_threshold` kwarg only — deliberately not a CLI flag.
DEFAULT_STALL_IDLE_THRESHOLD = 90.0

# Hard escalation bars sitting *above* the soft 90s WARN, which stays a
# terminal noop below them. Crossing a bar ends supervision with
# `reason: "worker_hung"` so the supervising session regains control; the
# `asl run` child is never killed or waited on — it stays alive with the lock
# held, so re-running the same command re-attaches. Overridable via kwargs
# only — deliberately not CLI flags.
#
# `DEFAULT_HANG_ESCALATION_PROBES`: consecutive heartbeat ticks whose snapshot
# carries a runner `hang_suspected` block of the result-received class before
# escalating (~60s at the 30s tick cadence). The runner already confirmed the
# harness result arrived, the process stays alive, and its tree burned no CPU
# across the confirmation samples, so two corroborating ticks are enough.
# `DEFAULT_HANG_IDLE_CEILING`: hard idle backstop (seconds); a tick at or above
# it escalates regardless of hang class, unless the runner's live
# `hang_cpu_progress` sample reports the tree progressing. Deliberately
# conservative so a legitimate long sleep/network wait below the ceiling never
# churns — the runner-side block and its AgentMux notification already
# surfaced the suspicion.
DEFAULT_HANG_ESCALATION_PROBES = 2
DEFAULT_HANG_IDLE_CEILING = 900.0

# Quota auto-resume bounds, in play only under `--wait-quota-reset`. Quota
# stays fail-closed at the policy layer (`pause_policy` still classifies
# `quota-exhausted` as `requires_user`); the supervisor opts in above it and
# these bound how far that opt-in may go. Overridable via kwargs only —
# deliberately not CLI flags, matching the tuning constants above.
#
# `DEFAULT_QUOTA_RESET_BUFFER_SECONDS`: slack added past the parsed reset
# instant. The hint is advisory free text a provider rounded, and clock skew
# plus reset propagation both push the real availability later, so relaunching
# exactly on it risks an immediate second rejection — which costs a whole
# resume. Five minutes is negligible against a multi-hour quota window.
#
# `DEFAULT_QUOTA_WAIT_CEILING_SECONDS`: refuse to sleep longer than this for a
# single reset. Eight days clears every real weekly quota window with room to
# spare; the bound exists to reject a nonsense hint (a timestamp a year out),
# not to second-guess a plausible one. Sized generously because stopping early
# costs a supervision gap where nobody is watching the plan, which is the
# expensive failure — the same reason `DEFAULT_ATTACH_MAX_SECONDS` is high.
#
# `DEFAULT_MAX_QUOTA_RESUMES`: how many resets one supervision may sleep
# through. A provider that keeps rejecting right after each reset burns one
# resume per attempt, so this is what converges a stuck provider to
# `user_required` instead of looping forever. Eight covers a plan that spans
# several days of quota windows.
#
# `QUOTA_WAIT_SLICE_SECONDS`: sleep granularity. The remaining time is
# recomputed from wall clock every slice rather than accumulated, so a machine
# suspend that jumps past the reset costs at most one slice of lateness instead
# of the whole suspended duration.
DEFAULT_QUOTA_RESET_BUFFER_SECONDS = 300.0
DEFAULT_QUOTA_WAIT_CEILING_SECONDS = 691200.0
DEFAULT_MAX_QUOTA_RESUMES = 8
QUOTA_WAIT_SLICE_SECONDS = 30.0

# Escalation classes carried in the `worker_hung` result's `hang.class`. The
# first two mirror the run's `hang_suspected.reason` values
# (`agent_session_loop.turn_execution.HANG_REASON_RESULT_RECEIVED` /
# `agent_session_loop.turn_execution.HANG_REASON_ZERO_CPU`); the ceiling class
# marks an idle overrun with no (or a different) runner block.
HANG_CLASS_RESULT_RECEIVED = "result_received_process_alive"
HANG_CLASS_ZERO_CPU = "zero_cpu_idle"
HANG_CLASS_IDLE_CEILING = "idle_ceiling_exceeded"

# Fixed operator guidance attached to every `worker_hung` result. Notify-only
# by default: the supervisor inspects and asks the human before any kill.
HANG_NEXT_STEPS = (
    "verify the tree with ps -axo pid,ppid,state,time,command vs "
    "hang_suspected.process_tree",
    "ask the user before killing anything",
    "if approved: kill only the identified leaf (deepest 0-CPU descendant), "
    "never asl run / runner",
    "re-run the same supervision command to re-attach",
)

# Pause classification is owned by `agent_session_loop.pause_policy`, the single
# source of truth for which pauses are recoverable, which require a human, the
# same-session retry budget, and the transport backoff schedule. These names are
# re-exported under their historical public identifiers so the supervision loop,
# the `test_asl_autopilot_*.py` suites, and the helper's JSON contract keep
# resolving them.
# The runner attaches `pause_policy.classify_pause(reason)` to its pause
# payloads; the supervisor prefers that payload block (see
# `_effective_pause_policy`) and falls back to these constants for older state.
RECOVERABLE_PAUSE_REASONS = _pause_policy.RECOVERABLE_PAUSE_REASONS
DEFAULT_MAX_RETRIES = _pause_policy.DEFAULT_MAX_RETRIES
USER_REQUIRED_HARNESS_FAILURE_REASONS = (
    _pause_policy.USER_REQUIRED_HARNESS_FAILURE_REASONS
)
TRANSPORT_RETRY_DELAYS_SECONDS = _pause_policy.TRANSPORT_RETRY_DELAYS_SECONDS
USER_ACTION_REPLY = _pause_policy.USER_ACTION_REPLY
classify_pause = _pause_policy.classify_pause

# Pauses whose answer is a decision this helper is structurally unable to make.
# `conversation-recovery-required` asks whether to discard an agent's saved
# reasoning; `redirect-successor-blocked` asks about a worker process that may
# still be alive beside the plan. Neither is in
# `pause_policy.RECOVERABLE_PAUSE_REASONS`, so the supervision loop already
# stops on both through the ordinary `requires_user` path and this set adds no
# control flow whatsoever. What it adds is the `recovery` block below: which of
# the two decisions the operator is being handed, which command reads the
# evidence for it, and — for the conversation case — the exact one-use relaunch
# that authorizes a fresh start. Without it a supervising agent has only a
# pause message to infer all three from, and the inference it would most
# naturally make is "relaunch and see", which is the one move that must never
# be made here.
#
# Aliased rather than declared. The mapping from pause reason to decision lives
# in `intervention` because more than one surface now reports these two pauses
# — this helper's `user_required` stop and the bridge's recovery projection —
# and a second copy here is how the two would come to name different decisions
# for the same pause.
RECOVERY_DECISION_PAUSE_REASONS: tuple[str, ...] = (
    _intervention.RECOVERY_DECISION_PAUSE_REASONS
)

# Which decision a recovery pause hands over. `fresh-conversation` is answered
# by an operator authorizing one fresh start; `predecessor-not-stopped` is
# answered by the machine, not by anyone's words.
RECOVERY_DECISION_FRESH_CONVERSATION = (
    _intervention.RECOVERY_DECISION_FRESH_CONVERSATION
)
RECOVERY_DECISION_PREDECESSOR_ALIVE = _intervention.RECOVERY_DECISION_PREDECESSOR_ALIVE

# Default automatic repair/rereview cycle cap, mirroring
# ``agent_session_loop.run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES``. The runner owns
# the repair cycles (Session 18): after a findings review it materializes
# same-plan repair children, executes them, and rereviews — bounded by
# ``asl run --max-review-fix-cycles`` (``--max-review-fix-plans`` is a
# deprecated alias). The helper only validates and forwards the flag; it never
# creates follow-up fix plans.
DEFAULT_MAX_REVIEW_FIX_CYCLES = _run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES

# Final review artifacts. When a run uses `--review-harness`, ASL writes the
# review verdict to `<plan-dir>/final-review.md` with a single trailing
# `Status:` line (and, for generated v2 review sessions, the structured
# `final-review.json` artifact). The grammar and artifact schema live in
# `agent_session_loop.review`; these re-exports keep the helper's historical
# public names stable.
FINAL_REVIEW_FILENAME = _review.FINAL_REVIEW_FILENAME
FINAL_REVIEW_ARTIFACT_FILENAME = _review.FINAL_REVIEW_ARTIFACT_FILENAME
REVIEW_STATUS_ALL_CLEAR = _review.REVIEW_STATUS_ALL_CLEAR

# Upper bound on the bounded review excerpt attached to a `review_findings`
# result. Keeps the structured payload small while still handing the host agent
# enough of `final-review.md` to surface the remaining findings to the user.
REVIEW_SUMMARY_MAX_CHARS = 2000
