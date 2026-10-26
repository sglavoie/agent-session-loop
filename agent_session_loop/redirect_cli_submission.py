# ruff: noqa: F403, F405
from .redirect_cli_projection import *
from .redirect_cli_projection import (
    _SUBMISSION_EXIT_CODES,
    _expected_target,
    _feedback_from_args,
    _live_target,
    _stage_for_submission,
    _store_error_details,
    _target_summary,
    _validated_timeout,
)
from .redirect_cli_vocab import *


def control_redirect_from_args(args: argparse.Namespace) -> int:
    """Submit one urgent correction and report what became of it."""
    context = cli_common.resolve_plan_context(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
    )
    if context is None:
        return EXIT_REFUSED
    plan_dir = context.snapshot.plan_dir
    state_dir = getattr(args, "state_dir", None)

    feedback = _feedback_from_args(args)
    if feedback is None:
        return EXIT_REFUSED
    wait_seconds = _validated_timeout(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
        default_seconds=redirect_admission.ACCEPTANCE_WAIT_SECONDS,
        maximum_seconds=redirect_admission.MAX_ACCEPTANCE_WAIT_SECONDS,
    )
    if wait_seconds is None:
        return EXIT_REFUSED
    expected_target, expectation_ok = _expected_target(
        args,
        plan_dir=plan_dir,
        live_target=_live_target(plan_dir, state_dir),
    )
    if not expectation_ok:
        return EXIT_REFUSED

    try:
        outcome = redirect_admission.submit_redirect(
            plan_dir,
            feedback,
            state_dir=state_dir,
            operation_id=getattr(args, "operation_id", None),
            ttl_seconds=getattr(args, "ttl", None),
            expected_target=expected_target,
            wait_seconds=wait_seconds,
        )
    except redirect_store_models.RedirectStoreError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=error.reason,
            error=error.message,
            details=_store_error_details(error),
        )
        return EXIT_REFUSED
    except redirect_admission.RedirectAdmissionError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason=error.reason,
            error=error.message,
            details=dict(error.details),
        )
        return EXIT_REFUSED
    except OSError as error:
        cli_common.print_error(
            args,
            command_name=COMMAND_REDIRECT,
            text_label=TEXT_LABEL_REDIRECT,
            reason="os-error",
            error=str(error),
        )
        return EXIT_REFUSED

    return _report_submission(args, outcome, expected_target=expected_target)


def _report_submission(
    args: argparse.Namespace,
    outcome: redirect_admission.SubmissionOutcome,
    *,
    expected_target: redirect_store_models.RedirectTarget | None,
) -> int:
    stage = _stage_for_submission(outcome)
    exit_code = _SUBMISSION_EXIT_CODES[outcome.outcome]
    submission = outcome.to_payload()
    submission["stage"] = stage
    submission["stage_description"] = STAGE_DESCRIPTIONS[stage]
    submission["expected_target"] = (
        expected_target.to_payload() if expected_target is not None else None
    )
    submission["exit_code"] = exit_code

    if exit_code == EXIT_OK:
        payload = responses.success_payload(COMMAND_REDIRECT, **submission)
        if args.json:
            responses.print_json(payload)
        else:
            target = outcome.target
            where = f" {_target_summary(target)}" if target is not None else ""
            print(
                f"Redirect {outcome.operation_id} accepted for{where} "
                f"({stage}: {STAGE_DESCRIPTIONS[stage]})"
            )
        return exit_code

    cli_common.print_error(
        args,
        command_name=COMMAND_REDIRECT,
        text_label=TEXT_LABEL_REDIRECT,
        reason=outcome.reason or outcome.outcome,
        error=_submission_error_text(outcome, stage),
        details=submission,
        hint=_submission_hint(outcome),
    )
    return exit_code


def _submission_error_text(
    outcome: redirect_admission.SubmissionOutcome,
    stage: str,
) -> str:
    subject = (
        f"redirect {outcome.operation_id}"
        if outcome.operation_id is not None
        else "redirect"
    )
    # The stage says what happened and the reason says why; text output used
    # to drop the reason, so a refusal with no live run read only "rejected".
    why = f" ({outcome.reason})" if outcome.reason else ""
    return f"{subject} is {stage}{why}: {STAGE_DESCRIPTIONS[stage]}"


def _submission_hint(outcome: redirect_admission.SubmissionOutcome) -> str | None:
    """Say what to do next, and in the one ambiguous case what not to do.

    An ``uncertain`` submission is the case worth spending words on: the request
    is persisted, inside its window, and may yet be accepted, so the failure mode
    is an operator who reads "not accepted" and sends the correction again --
    which is how one correction becomes two.
    """
    if outcome.outcome == redirect_admission.OUTCOME_UNCERTAIN:
        return RECONCILE_HINT
    if outcome.outcome == redirect_admission.OUTCOME_UNAVAILABLE:
        return (
            "the request is durably settled, so no later run will act on it; "
            "resubmit once a run holds this plan again"
        )
    if outcome.reason == redirect_admission.REFUSE_RUN_PAUSED:
        return "a paused run is answered through the mailbox, not interrupted"
    if outcome.reason == redirect_admission.REFUSE_STALE_TARGET:
        return "the live turn is not the one named; re-read `asl status` and resubmit"
    return None


# ---------------------------------------------------------------------------
# asl control redirect-status
# ---------------------------------------------------------------------------
