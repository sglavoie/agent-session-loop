# ruff: noqa: F401, I001
"""Facade over the control service implementation shared by its callers."""

from __future__ import annotations

from .control_service_part_1 import (
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
from .control_service_part_2 import (
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
from .control_service_part_3 import (
    submit_correction,
    RedirectStatusView,
    status_view,
    RecoveryView,
    recovery_view,
)

from . import control_service_shared as _shared
from . import control_service_part_1 as _control_service_part_1
from . import control_service_part_2 as _control_service_part_2
from . import control_service_part_3 as _control_service_part_3

_PARTS = (
    _control_service_part_1,
    _control_service_part_2,
    _control_service_part_3,
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
