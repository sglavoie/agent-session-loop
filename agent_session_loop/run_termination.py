# ruff: noqa: F401, I001
"""Compatibility facade for run terminal, pause, and control behavior."""

from __future__ import annotations

from .run_termination_controls import (
    _detach_turn_controls,
    _honor_abort_turn,
    _honor_stop_after_current,
    _record_abort_preempted,
    _record_abort_satisfied_by_worker,
    _record_aborted_turn_exit,
    _stop_before_review_work,
)
from .run_termination_pause import (
    _PAUSE_REMEDIATION_PATHS,
    _commit_failed_remediation_message,
    _optional_str,
    _pause_remediation,
    _pause_run,
    _registered_repository_root,
    _runtime_reason_remediation,
    _supersede_live_action_signal,
    set_pause,
)
from .run_termination_shared import (
    ExistingReviewVerdictRecorder,
    ReadinessStateWriter,
    SessionStatusReverter,
    SnapshotReinspector,
    _UNAUDITED_TERMINAL_OUTCOMES,
    readiness_finalizer,
)
from .run_termination_terminal import (
    _TerminalAdjudication,
    _adjudicate_terminal_run,
    _attach_terminal_assurance,
    _audited_repositories,
    _final_readiness_state_from_payload,
    _finalize_readiness_lifecycle,
    _terminal_outcome,
    _terminalize_run,
)
