# mypy: disable-error-code="name-defined,attr-defined,no-redef"
# ruff: noqa: E402, F403, F405, I001
from . import workbench_controls_shared as _shared

globals().update(
    {name: value for name, value in vars(_shared).items() if not name.startswith("__")}
)
from .workbench_controls_shared import *
from .workbench_controls_part_1 import *
from .workbench_controls_part_2 import *


def submit_correction(
    plan_dir: str | Path,
    *,
    operation_id: str,
    text: Any,
    state_dir: str | Path | None = None,
    expected_target_raw: Any = None,
    wait_seconds_raw: Any = None,
    authorization: str = portable_controls.AUTHORIZATION_OPEN,
    action_token: str | None = None,
    run_id: str | None = None,
    target_label: str = "expected_target",
    wait_label: str = "wait_seconds",
) -> RedirectSubmission:
    """Submit one urgent correction against the turn this run is on now.

    Replay is served out of the redirect journal *before* the freshness proof is
    checked, and that ordering is the whole reason this request may be retried.
    A correction that landed moves the plan, which moves the action token, which
    would make the retry of a request whose response was lost look like a stale
    second correction. Asking the journal first makes the ``operation_id`` the
    client already holds the answer.

    What is *not* short-circuited is the request itself. The text and any named
    turn are compared against the record that id already names, because a retry
    is only a retry when it carries the same correction: an operator whose
    response was lost still has an editable field, and reporting their edited
    words as a successful replay of the words the worker actually got is the one
    falsehood this whole path exists to avoid.

    Under ``AUTHORIZATION_STRICT`` the plan is read again immediately before the
    submission, so the fingerprint a correction is checked against is the one
    the journal is about to be appended under rather than the one the request
    arrived on.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    try:
        correction = portable_controls.require_correction_text(text)
    except portable_controls.ControlRequestError as error:
        raise _refuse_request(error, plan_dir=facts.plan_dir) from error
    target = expected_target(expected_target_raw, label=target_label)

    recorded = recorded_redirect(facts.plan_dir, operation_id, state_dir=state_dir)
    if recorded is not None:
        replay = _submission(
            facts=facts,
            state_dir=state_dir,
            operation_id=operation_id,
            outcome=None,
            reason=None,
            target=None,
            waited_seconds=0.0,
            replayed=True,
            persisted=True,
            accepted=False,
        )
        conflict = redirect_store_validation.replay_conflict(
            recorded.feedback_digest,
            recorded.target,
            feedback=correction,
            expected_target=target,
        )
        if conflict is not None:
            raise RedirectConflict(
                conflict,
                operation_id=operation_id,
                submission=replay,
                plan_dir=facts.plan_dir,
            )
        return replay

    if authorization == portable_controls.AUTHORIZATION_STRICT:
        facts = run_facts(plan_dir, state_dir=state_dir)
        if not facts.run_active:
            raise ControlRefused(
                intervention.REASON_NO_ACTIVE_RUN,
                intervention.DISABLED_NO_ACTIVE_RUN,
                scope=portable_controls.SCOPE_INTERVENTION,
                plan_dir=facts.plan_dir,
            )
    try:
        intervention.validate_action(
            facts,
            intervention.ACTION_REDIRECT,
            token=action_token,
            run_id=run_id,
        )
    except intervention.InterventionError as error:
        raise _refuse_intervention(error, plan_dir=facts.plan_dir) from error

    waited = wait_seconds(wait_seconds_raw, label=wait_label)
    try:
        outcome = redirect_admission.submit_redirect(
            facts.plan_dir,
            correction,
            state_dir=state_dir,
            operation_id=operation_id,
            expected_target=target,
            wait_seconds=waited,
        )
    except redirect_admission.RedirectAdmissionError as error:
        raise RedirectSubmitFailed(
            error.reason,
            error.message,
            source=portable_controls.SOURCE_ADMISSION,
            request_shaped=True,
            details=dict(error.details),
            plan_dir=facts.plan_dir,
        ) from error
    except redirect_store_models.RedirectStoreError as error:
        # Split on the journal's own reason rather than on the exception type:
        # one exception covers both "this body is not a correction" and "this
        # journal cannot be written", and calling an oversized correction a
        # retryable server fault would send a client back to resend the one
        # thing that cannot succeed.
        raise RedirectSubmitFailed(
            error.reason,
            error.message,
            source=portable_controls.SOURCE_JOURNAL,
            request_shaped=error.reason in REDIRECT_REQUEST_REASONS,
            details=dict(error.details),
            plan_dir=facts.plan_dir,
        ) from error
    except OSError as error:
        raise RedirectSubmitFailed(
            "os-error",
            str(error),
            source=portable_controls.SOURCE_IO,
            request_shaped=False,
            plan_dir=facts.plan_dir,
        ) from error

    return _submission(
        facts=facts,
        state_dir=state_dir,
        operation_id=outcome.operation_id,
        outcome=outcome.outcome,
        reason=outcome.reason,
        target=outcome.target.to_payload() if outcome.target is not None else None,
        waited_seconds=outcome.waited_seconds,
        replayed=False,
        persisted=outcome.persisted,
        accepted=outcome.accepted,
    )


# --------------------------------------------------------------------------
# Redirect status and recovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RedirectStatusView:
    """This plan's durable redirect state, projected but not yet shaped.

    ``truncated`` is the window's honesty flag and ``report.complete`` is the
    journal's: a torn journal means a record may be missing from the source, and
    a full window means one is missing from this answer. A reader that is told
    only one of them can mistake a bounded view for a complete one.
    """

    facts: intervention.RunFacts
    report: redirect_recovery.RecoveryReport
    live_target: redirect_store_models.RedirectTarget | None
    operations: tuple[redirect_recovery.OperationOutcome, ...]
    truncated: bool

    def operation_payloads(self) -> list[dict[str, Any]]:
        """Project every operation in the window the way every reader does."""
        return [
            redirect_cli.operation_payload(operation, live_target=self.live_target)
            for operation in self.operations
        ]


def status_view(
    plan_dir: str | Path,
    *,
    state_dir: str | Path | None = None,
    operation_ids: Sequence[str] = (),
) -> RedirectStatusView:
    """Project this plan's durable redirect state, writing nothing.

    A read, and available without authorization on both surfaces, because the
    client most in need of it is the one that just lost its own record of what
    it submitted -- including one whose freshness proof is long stale. The proof
    a correction must carry back is handed out *by* this read, so a composer
    never has to guess at one.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    wanted = frozenset(value for value in operation_ids if value.strip())
    report = redirect_report(facts.plan_dir, state_dir=state_dir)
    selected = [
        operation
        for operation in report.operations
        if not wanted or operation.operation_id in wanted
    ]
    window = portable_controls.newest_operations(selected)
    return RedirectStatusView(
        facts=facts,
        report=report,
        live_target=live_redirect_target(facts.plan_dir, state_dir=state_dir),
        operations=tuple(window),
        truncated=len(selected) > len(window),
    )


@dataclass(frozen=True)
class RecoveryView:
    """Which recovery a pause is waiting on, and how to authorize it.

    A read, and deliberately only a read on both surfaces. Performing the
    recovery through either would persist the operator's one-use authorization
    in the one place it must never live: a request that can be replayed.
    """

    facts: intervention.RunFacts
    report: redirect_recovery.RecoveryReport
    live_target: redirect_store_models.RedirectTarget | None
    decision: intervention.RecoveryDecision
    inspect_command: str | None
    authorization_command: str | None
    retained: tuple[redirect_recovery.OperationOutcome, ...]

    @property
    def one_use(self) -> bool:
        """Whether there is an authorization command, and it may be used once.

        Both fields describe the same command, so both are false and null where
        there is none: telling an operator that an absent authorization is
        one-use would read as an invitation to go find it.
        """
        return self.authorization_command is not None

    @property
    def note(self) -> str | None:
        return redirect_cli.FRESH_CONVERSATION_NOTE if self.one_use else None

    def retained_payloads(self) -> list[dict[str, Any]]:
        """The corrections a fresh start would still carry.

        Reported with the recovery so an operator authorizing one sees what it
        inherits rather than discovering it in the successor's first prompt.
        """
        return [
            redirect_cli.operation_payload(operation, live_target=self.live_target)
            for operation in self.retained
        ]


def recovery_view(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> RecoveryView:
    """Read what a paused plan needs from a person, and what would carry over.

    The pause-to-decision mapping and both commands come from
    :mod:`.intervention` and :mod:`.redirect_cli` rather than being reassembled,
    so a recovery card, a bridge frame, and ``asl control recover-conversation``
    cannot hand an operator three different answers about one pause.
    """
    facts = run_facts(plan_dir, state_dir=state_dir)
    pause_reason = facts.runner_state.get("reason")
    decision = intervention.recovery_decision(
        pause_reason if isinstance(pause_reason, str) else None,
        paused=facts.paused,
    )
    commands = redirect_cli.recovery_commands(
        facts.plan_dir,
        str(state_dir) if state_dir is not None else None,
        decision=decision.decision,
    )
    report = redirect_report(facts.plan_dir, state_dir=state_dir)
    return RecoveryView(
        facts=facts,
        report=report,
        live_target=live_redirect_target(facts.plan_dir, state_dir=state_dir),
        decision=decision,
        inspect_command=commands["inspect_command"],
        authorization_command=commands["authorization_command"],
        retained=tuple(
            portable_controls.newest_operations((*report.unfinished, *report.uncertain))
        ),
    )
