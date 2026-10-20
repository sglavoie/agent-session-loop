from .asl_live_redirect_availability import _read_lines
from .asl_live_redirect_prompts import (
    _mentions_conversation,
    correction_text,
    first_turn_prompt,
    native_cancel_prompt,
    provider_refusal,
    resume_turn_prompt,
    unknown_history_prompt,
)
from .asl_live_redirect_support import *
from .asl_live_redirect_workspace import Workspace, _LiveTurn, disposable_workspace


def _seed_correction(
    workspace: Workspace,
    *,
    harness: HarnessName,
    run_id: str,
) -> tuple[
    redirect_store_models.RedirectTarget,
    redirect_store_models.RedirectTarget,
    str,
    list[str],
]:
    """Submit, accept, and carry one correction through the real journal.

    Returns the interrupted turn's target, the continuation attempt, the
    operation id, and the correction block exactly as the runner would render
    it into a resumed prompt.
    """
    target = redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id="live-acceptance",
        turn_index=1,
        harness=harness,
    )
    continuation = redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id="live-acceptance",
        turn_index=2,
        harness=harness,
    )
    request = redirect_store_requests.submit_request(
        workspace.plan_dir,
        target=target,
        feedback=correction_text(),
        state_dir=workspace.state_dir,
        ttl_seconds=CORRECTION_TTL_SECONDS,
    )
    redirect_store_requests.accept_request(
        workspace.plan_dir,
        request.operation_id,
        target=target,
        state_dir=workspace.state_dir,
    )
    carried = redirect_ack_carry.carry_corrections(
        workspace.plan_dir,
        continuation=continuation,
        accepted_operation_ids=(request.operation_id,),
        state_dir=workspace.state_dir,
    )
    block = run_prompts._correction_context_body(
        carried,
        plan_dir=workspace.plan_dir,
        state_dir=workspace.state_dir,
    )
    return target, continuation, request.operation_id, list(block)


def _interrupt_and_continue(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Interrupt a working turn, then resume it carrying a real correction.

    One workspace and one conversation across two turns, because that is the
    only shape in which "earlier context survived" and "the edits are still
    there" are answerable at all.
    """
    observed: dict[str, Any] = {}
    secret = f"PASSPHRASE-{uuid.uuid4().hex[:12].upper()}"
    nonce = uuid.uuid4().hex[:12].upper()
    run_id = f"live-{uuid.uuid4().hex[:12]}"
    with disposable_workspace(f"{harness}-continue", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)

        def watch(live: _LiveTurn) -> None:
            if not live.wait_for_file(RUNNING_FILE, TOOL_START_TIMEOUT_SECONDS):
                live.abort.arm("live-acceptance-tool-never-started")
                return
            time.sleep(INTERRUPT_SETTLE_SECONDS)
            live.collect_evidence()
            live.abort.arm(ABORT_REASON)

        first = turn.run(first_turn_prompt(secret, nonce), watch=watch)
        observed["first_turn"] = first.summary()
        tool_started = (workspace.repo / RUNNING_FILE).exists()

        matrix.verdict(
            SCENARIO_STOPS,
            bool(
                tool_started
                and first.result.aborted
                and first.result.returncode == 137
                and first.result.cancel_stage == harnesses.CANCEL_STAGE_EXECUTION
            ),
            (
                "the abort ended the turn in the execution window"
                if first.result.aborted
                else "the turn was not stopped by the abort"
            ),
            seconds=first.seconds,
            evidence={
                "tool_started": tool_started,
                "returncode": first.result.returncode,
                "cancel_stage": first.result.cancel_stage,
                "aborted": first.result.aborted,
                "released": first.released,
                # Which request was made, so "nothing armed the abort" is never
                # read as "the harness ignored it".
                "abort_reason": turn.abort.reason or None,
                "timed_out": first.result.timed_out,
                "final_message": first.final_message,
            },
        )

        collected = first.descendant_evidence or {}
        # The walk records the group it actually saw; the spawn pid is only a
        # fallback for the case where the worker exited before it was sampled.
        pgid = collected.get("root_pgid") or first.spawn_pid or 0
        confirmation = process_tree.confirm_termination(
            pgid=int(pgid), evidence=collected
        )
        evidence_collected = bool(collected.get("collected"))
        matrix.verdict(
            SCENARIO_FORCED,
            bool(first.released and evidence_collected and confirmation["complete"]),
            (
                "the worker group and its tracked descendants are confirmed stopped"
                if confirmation["complete"]
                else "termination could not be confirmed: "
                f"{confirmation['incomplete_reason']}"
            ),
            seconds=first.seconds,
            evidence={
                "evidence_collected": evidence_collected,
                "tracked_descendants": len(collected.get("entries") or ()),
                "escaped_pids": list(collected.get("escaped_pids") or ()),
                "confirmation": confirmation,
                "released": first.released,
            },
        )

        notes = workspace.repo / NOTES_FILE
        notes_text = notes.read_text(encoding="utf-8") if notes.exists() else ""
        matrix.verdict(
            SCENARIO_EDITS,
            f"NONCE={nonce}" in notes_text,
            (
                f"{NOTES_FILE} written before the interruption survived it"
                if f"NONCE={nonce}" in notes_text
                else f"{NOTES_FILE} did not survive with its recorded nonce"
            ),
            evidence={
                "file": NOTES_FILE,
                "present": notes.exists(),
                "final_message": first.final_message,
            },
        )

        conversation_id = first.conversation_id
        observed["conversation_id"] = conversation_id
        if not conversation_id:
            matrix.record(
                SCENARIO_CONTEXT,
                STATUS_FAILED,
                "the interrupted turn named no conversation to continue",
            )
            matrix.record(
                SCENARIO_CORRECTION,
                STATUS_FAILED,
                "no conversation was captured, so nothing could be continued",
            )
            return observed

        _target, continuation, operation_id, block = _seed_correction(
            workspace, harness=harness, run_id=run_id
        )
        resumed_turn = _LiveTurn(harness, workspace)
        second = resumed_turn.run(
            resume_turn_prompt(block),
            watch=None,
            resume_conversation_id=conversation_id,
        )
        observed["resumed_turn"] = second.summary()
        receipt = _read_lines(workspace.repo / RESUMED_FILE)

        matrix.verdict(
            SCENARIO_CONTEXT,
            receipt.get("SECRET") == secret,
            (
                "the resumed turn recalled a passphrase that was never on disk"
                if receipt.get("SECRET") == secret
                else "the resumed turn did not recall the prior turn's passphrase"
            ),
            seconds=second.seconds,
            evidence={
                "resumed_conversation": second.conversation_id,
                "requested_conversation": conversation_id,
                "secret_recalled": receipt.get("SECRET") == secret,
                "secret_reported_unknown": receipt.get("SECRET") == "UNKNOWN",
                "returncode": second.result.returncode,
                "final_message": second.final_message,
            },
        )

        echoed_attempt = receipt.get("ATTEMPT", "")
        echoed_operation = receipt.get("OPERATION", "")
        acknowledged = False
        ack_detail = "the worker echoed no attempt token"
        if echoed_attempt and echoed_operation == operation_id:
            try:
                redirect_ack.acknowledge_correction(
                    workspace.plan_dir,
                    operation_id,
                    token=echoed_attempt,
                    live_attempt=continuation,
                    state_dir=workspace.state_dir,
                )
            except redirect_ack_attempt.RedirectAckError as error:
                ack_detail = f"the journal refused the receipt: {error}"
            else:
                acknowledged = True
                ack_detail = (
                    "the correction reached the resumed worker and the journal "
                    "accepted its receipt"
                )
        elif echoed_operation and echoed_operation != operation_id:
            ack_detail = "the worker echoed a different operation id"

        matrix.verdict(
            SCENARIO_CORRECTION,
            acknowledged,
            ack_detail,
            seconds=second.seconds,
            evidence={
                "operation_id": operation_id,
                "attempt": redirect_ack_attempt.attempt_token(continuation),
                "echoed_attempt": echoed_attempt,
                "echoed_operation": echoed_operation,
                "acknowledged": acknowledged,
                "final_message": second.final_message,
            },
        )
        return observed


def _native_cancellation(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Stop a turn through the harness's own protocol, with no forced kill.

    The kill is held back rather than removed: a harness that ignores its own
    interruption would otherwise hang this check for the whole turn timeout,
    and the point is to learn *which* of the two stopped it.
    """
    observed: dict[str, Any] = {}
    with disposable_workspace(f"{harness}-cancel", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        answered: list[bool] = []
        ended_natively: list[bool] = []

        def watch(live: _LiveTurn) -> None:
            if not live.wait_for_file(RUNNING_FILE, TOOL_START_TIMEOUT_SECONDS):
                live.abort.arm("live-acceptance-tool-never-started")
                return
            time.sleep(INTERRUPT_SETTLE_SECONDS)
            answered.append(live.request_native_interrupt())
            # The safety net: if the harness's own cancellation does not end
            # the turn, arm the abort so the check reports a failure instead of
            # waiting out the turn timeout.
            if live.abort.wait_completed(NATIVE_CANCEL_WAIT_SECONDS):
                ended_natively.append(True)
                return
            live.abort.arm("live-acceptance-native-cancel-safety-net")

        run = turn.run(native_cancel_prompt(), watch=watch)
        observed["turn"] = run.summary()
        tool_started = (workspace.repo / RUNNING_FILE).exists()
        interrupt_answered = bool(answered and answered[0])
        # A turn that merely ran out its timeout also carries `aborted=False`,
        # so "no kill was needed" has to exclude it or the laziest possible
        # outcome would score as the harness cancelling cleanly.
        stopped_without_kill = not run.result.aborted and not run.result.timed_out
        matrix.verdict(
            SCENARIO_NATIVE_CANCEL,
            bool(tool_started and interrupt_answered and stopped_without_kill),
            (
                "the harness's own interruption ended the turn without a kill"
                if stopped_without_kill and interrupt_answered
                else "native cancellation did not end the turn on its own"
            ),
            seconds=run.seconds,
            evidence={
                "tool_started": tool_started,
                "transport": run.result.transport,
                "interrupt_answered": interrupt_answered,
                "forced_kill_needed": run.result.aborted,
                "returncode": run.result.returncode,
                "cancel_stage": run.result.cancel_stage,
                "abort_reason": turn.abort.reason or None,
                "timed_out": run.result.timed_out,
                "final_message": run.final_message,
            },
        )
        return observed


def _unavailable_history(
    harness: HarnessName, matrix: _Matrix, *, keep: bool
) -> dict[str, Any]:
    """Resume a conversation the harness has never heard of."""
    observed: dict[str, Any] = {}
    unknown = str(uuid.uuid4())
    with disposable_workspace(f"{harness}-absent", keep=keep) as workspace:
        turn = _LiveTurn(harness, workspace)
        try:
            run = turn.run(
                unknown_history_prompt(),
                watch=None,
                resume_conversation_id=unknown,
                timeout=120,
            )
        except harnesses.HarnessError as error:
            # A refusal only counts when it is *about the conversation*. A
            # setup error — a missing plan directory, an unusable workspace —
            # raises the same exception type and would otherwise be scored as a
            # pass, which would make this the one row in the matrix that gets
            # easier to satisfy the more broken the environment is.
            about_history = _mentions_conversation(str(error), unknown)
            matrix.verdict(
                SCENARIO_HISTORY,
                about_history,
                (
                    f"the adapter refused the unknown conversation: {error}"
                    if about_history
                    else f"the turn could not be set up, so nothing was "
                    f"learned about missing history: {error}"
                ),
                evidence={
                    "refused_by": "adapter" if about_history else None,
                    "conversation_id": unknown,
                    "error": str(error),
                },
            )
            return observed
        observed["turn"] = run.summary()
        ran = (workspace.repo / FORBIDDEN_FILE).exists()
        # Same rule as the exception path: the run has to have stopped *and*
        # said the conversation is why.
        blamed = bool(run.result.conversation_error) or _mentions_conversation(
            f"{run.result.stderr}\n{run.result.stdout}", unknown
        )
        refused = run.result.returncode != 0 and blamed
        matrix.verdict(
            SCENARIO_HISTORY,
            bool(refused and not ran),
            (
                "an unknown conversation was refused rather than replaced"
                if refused and not ran
                else "the turn did not stop and blame the missing conversation"
            ),
            seconds=run.seconds,
            evidence={
                "requested_conversation": unknown,
                "returncode": run.result.returncode,
                "conversation_error": run.result.conversation_error,
                "continuity_unavailable": run.result.continuity_unavailable,
                "blamed_the_conversation": blamed,
                "work_performed": ran,
                "policy_note": (
                    "the pause-for-user-input half of this rule is deterministic "
                    "and is checked by tests.test_asl_harness_compatibility"
                ),
                "final_message": run.final_message,
            },
        )
        return observed
