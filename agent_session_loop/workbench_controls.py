# ruff: noqa: E402, F401, F403, F405, I001
"""Compatibility facade for the workbench controls implementation."""

from __future__ import annotations

from .workbench_controls_part_1 import (
    ABORT_NOTES,
    REDIRECT_REQUEST_REASONS,
    ControlRefused,
    ControlQueueRefused,
    RedirectSubmitFailed,
    PlanUnreadable,
    RedirectConflict,
    ControlUnavailable,
    _refuse_request,
    _refuse_intervention,
    ControlActivity,
    ControlOutcome,
    inspect_activity,
    _no_runner,
    activity_from_facts,
    require_harness,
    dispatch_control,
)
from .workbench_controls_part_2 import (
    _queue_command,
    _authorize_control,
    _queue_refusal,
    run_facts,
    redirect_report,
    live_redirect_target,
    recorded_redirect,
    RedirectSubmission,
    _submission,
    expected_target,
    wait_seconds,
)
from .workbench_controls_part_3 import (
    submit_correction,
    RedirectStatusView,
    status_view,
    RecoveryView,
    recovery_view,
)

from . import workbench_controls_shared as _shared
from . import workbench_controls_part_1 as _workbench_controls_part_1
from . import workbench_controls_part_2 as _workbench_controls_part_2
from . import workbench_controls_part_3 as _workbench_controls_part_3

_PARTS = (
    _workbench_controls_part_1,
    _workbench_controls_part_2,
    _workbench_controls_part_3,
)
for _part in _PARTS:
    globals().update(
        {
            name: value
            for name, value in vars(_part).items()
            if not name.startswith("__")
        }
    )
for _part in _PARTS:
    _part.__dict__.update(
        {name: value for name, value in globals().items() if not name.startswith("__")}
    )
del _part
