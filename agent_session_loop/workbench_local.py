"""Local Workbench provider assembled from read, launch, and control operations."""

from .workbench_local_controls import _ControlOperations
from .workbench_local_core import *
from .workbench_local_launch import _LaunchOperations
from .workbench_local_reads import _ReadOperations


class LocalProvider(_ReadOperations, _LaunchOperations, _ControlOperations):
    """Carry out one Workbench operation against this machine's ASL services."""

    mode = MODE_LOCAL

    def __init__(
        self, config: LocalConfig, *, launches: LaunchBook | None = None
    ) -> None:
        self.config = config
        self.launches = launches if launches is not None else LaunchBook()

    def unavailable(self, operation: str) -> str | None:
        return _LOCAL_UNAVAILABLE(operation)

    def invoke(self, call: Call) -> Response:
        handler = _HANDLERS.get(call.operation)
        if handler is None:  # pragma: no cover - route registration protects this
            raise HttpRefusal(
                500, "internal-error", f"no local handler for {call.operation!r}"
            )
        try:
            return handler(self, call)
        except workbench_archive.ArchiveRefused as error:
            raise HttpRefusal(
                error.status, error.reason, error.message, details=error.details
            ) from error
        except workbench_controls.ControlRefused as error:
            raise HttpRefusal(
                _refusal_status(error),
                error.reason,
                error.message,
                details=(
                    {"plan_dir": str(error.plan_dir), **error.details}
                    if error.plan_dir is not None
                    else dict(error.details)
                ),
                extra={"retryable": error.retryable},
            ) from error
        except intervention.InterventionError as error:
            raise HttpRefusal(
                _intervention_status(error.reason),
                error.reason,
                error.message,
                details=dict(error.details),
                extra={"retryable": error.retryable},
            ) from error


_HANDLERS: Mapping[str, Callable[[LocalProvider, Call], Response]] = {
    "health": LocalProvider._handle_health,
    "summary": LocalProvider._handle_summary,
    "queue": LocalProvider._handle_queue,
    "history": LocalProvider._handle_history,
    "plan": LocalProvider._handle_plan,
    "recovery": LocalProvider._handle_recovery,
    "registry": LocalProvider._handle_registry,
    "artifacts": LocalProvider._handle_artifacts,
    "state": LocalProvider._handle_state,
    "events": LocalProvider._handle_events,
    "logs": LocalProvider._handle_logs,
    "last-message": LocalProvider._handle_last_message,
    "result": LocalProvider._handle_result,
    "verification": LocalProvider._handle_verification,
    "commits": LocalProvider._handle_commits,
    "progress": LocalProvider._handle_progress,
    "final-review": LocalProvider._handle_final_review,
    "questions": LocalProvider._handle_questions,
    "redirect-status": LocalProvider._handle_redirect_status,
    "conversation-recovery": LocalProvider._handle_conversation_recovery,
    "archive-list": LocalProvider._handle_archive_list,
    "registry-pin": LocalProvider._handle_registry_pin,
    "registry-unpin": LocalProvider._handle_registry_unpin,
    "plan-archive": LocalProvider._handle_plan_archive,
    "plans-archive": LocalProvider._handle_plans_archive,
    "archive-restore": LocalProvider._handle_archive_restore,
    "archive-delete": LocalProvider._handle_archive_delete,
    "archive-settings": LocalProvider._handle_archive_settings,
    "launch": LocalProvider._handle_launch,
    "reply": LocalProvider._handle_reply,
    "question-reply": LocalProvider._handle_question_reply,
    "control-stop-after-current": LocalProvider._handle_control_stop_after_current,
    "control-abort": LocalProvider._handle_control_abort,
    "control-select-harness": LocalProvider._handle_control_select_harness,
    "control-redirect": LocalProvider._handle_control_redirect,
    "unlock": LocalProvider._handle_unlock,
}

_NOT_LOCAL: Mapping[str, str] = {
    "capabilities": "the application answers this itself",
    "ingest": "this host is the connector, not a service a connector uploads to",
    "command-redeem": "this host holds no remote command bodies to redeem",
}

_LOCAL_UNAVAILABLE = unavailable_from(
    _HANDLERS,
    reasons=dict(_NOT_LOCAL),
    default="this operation is not implemented by the local provider",
)

_MISSING_HANDLERS = tuple(
    sorted(
        operation
        for operation in HTTP_OPERATIONS
        if operation not in _NOT_LOCAL and operation not in _HANDLERS
    )
)
if _MISSING_HANDLERS:  # pragma: no cover - import-time route registration error
    raise RuntimeError(
        "workbench_local has no handler for: " + ", ".join(_MISSING_HANDLERS)
    )
