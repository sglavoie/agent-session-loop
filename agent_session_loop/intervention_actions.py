# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Action availability and freshness validation for ASL interventions."""

from __future__ import annotations

from . import intervention_facts as _facts

globals().update(
    {name: value for name, value in vars(_facts).items() if not name.startswith("__")}
)
del _facts


def inspect_run(
    plan_dir: str | Path, *, state_dir: str | Path | None = None
) -> RunFacts:
    """Read the canonical intervention facts for one plan without writing.

    Reaches the lock through ``state.inspect_lock_info`` so a dead pid's lock is
    classified rather than reclaimed: validating an action must never be the
    thing that changes the corpus being validated.
    """
    canonical = _canonical_plan_dir(plan_dir)
    lock_info = state_locks.inspect_lock_info(canonical, state_dir)
    state_path = state.plan_state_dir(canonical, state_dir) / events.STATE_FILE_NAME
    runner_state = state_payloads._runner_state_payload(state_path, lock_info=lock_info)
    run_id = runner_state.get("run_id")
    mailbox_view: mailbox_snapshot.MailboxView | None = None
    unanswered: tuple[mailbox_models.MailboxMessage, ...] = ()
    try:
        mailbox_view = mailbox.read_mailbox(canonical, state_dir=state_dir)
        unanswered = tuple(
            message
            for message in mailbox_view.unanswered_questions
            if isinstance(run_id, str) and run_id and message.run_id == run_id
        )
    except OSError, ValueError, mailbox_models.MailboxError:
        unanswered = ()
    active_wait_question_id: str | None = None
    active_turn = runner_state.get("active_turn")
    if mailbox_view is not None and isinstance(active_turn, Mapping):
        session_value = active_turn.get("session_id") or active_turn.get("session")
        session_id = (
            session_value if isinstance(session_value, str) and session_value else None
        )
        turn_index = active_turn.get("turn_index")
        if (
            isinstance(run_id, str)
            and run_id
            and session_id is not None
            and isinstance(turn_index, int)
            and not isinstance(turn_index, bool)
        ):
            active_wait = mailbox_view.active_worker_wait(
                run_id=run_id,
                session_id=session_id,
                turn_index=turn_index,
            )
            if active_wait is not None:
                active_wait_question_id = active_wait.to
    return build_run_facts(
        canonical,
        runner_state=runner_state,
        lock_active=bool(lock_info is not None and lock_info.active),
        unanswered_ids=tuple(message.id for message in unanswered),
        active_wait_question_id=active_wait_question_id,
    )


def permitted_actions(facts: RunFacts) -> tuple[str, ...]:
    """Return the actions a caller may take against this run right now."""
    allowed: list[str] = []
    live_question = (
        facts.run_active
        and facts.context_kind == "waiting"
        and facts.question_id is not None
    )
    if live_question:
        allowed.append(ACTION_REPLY)
        allowed.append(ACTION_RESOLVE)
    if facts.paused:
        allowed.append(ACTION_RESUME)
    if facts.run_active:
        allowed.append(ACTION_STOP_AFTER_CURRENT)
        allowed.append(ACTION_ABORT)
    if facts.run_active and facts.turn_active:
        # Deliberately coarse: a correction needs a turn to interrupt, and that
        # is the whole of what this layer can establish without writing. Every
        # finer refusal -- a terminal audit, a target that moved, an urgent
        # channel that is not listening -- is a fact about the runner that only
        # admission can read, and which would be stale by the time a caller
        # acted on it anyway. Mirrors `run.resume`, which is permitted on a
        # paused run and can still fail at launch.
        allowed.append(ACTION_REDIRECT)
    allowed.append(ACTION_WORKBENCH)
    return tuple(action for action in ACTIONS if action in allowed)


def disabled_reasons(facts: RunFacts) -> dict[str, str]:
    """Explain, per unavailable action, why it is unavailable."""
    allowed = set(permitted_actions(facts))
    reasons: dict[str, str] = {}
    for action in (ACTION_REPLY, ACTION_RESOLVE):
        if action in allowed:
            continue
        if not facts.run_active:
            reasons[action] = (
                DISABLED_PAUSED if facts.paused else DISABLED_NO_ACTIVE_RUN
            )
        else:
            reasons[action] = DISABLED_NO_LIVE_QUESTION
    if ACTION_RESUME not in allowed:
        reasons[ACTION_RESUME] = DISABLED_NOT_PAUSED
    for action in (ACTION_STOP_AFTER_CURRENT, ACTION_ABORT):
        if action not in allowed:
            reasons[action] = (
                DISABLED_PAUSED if facts.paused else DISABLED_NO_ACTIVE_RUN
            )
    if ACTION_REDIRECT not in allowed:
        if not facts.run_active:
            reasons[ACTION_REDIRECT] = (
                DISABLED_PAUSED if facts.paused else DISABLED_NO_ACTIVE_RUN
            )
        else:
            reasons[ACTION_REDIRECT] = DISABLED_NO_ACTIVE_TURN
    return reasons


def action_token(facts: RunFacts) -> str:
    """Return the freshness proof a caller must return with a mutation."""
    return facts.fingerprint()


def selected_question_id(facts: RunFacts, question_id: str | None) -> str | None:
    """Return the question a closure must be appended against, or ``None``.

    An omitted id means the run's primary question. An explicit id is honored
    whenever it is still in the canonical unanswered set, because a supervision
    client legitimately answers open questions in whatever order it chooses;
    only an id that is no longer open at all is refused.
    """
    if question_id is None or question_id == facts.question_id:
        return facts.question_id
    if question_id in facts.unanswered_ids:
        return question_id
    return None


def validate_action(
    facts: RunFacts,
    action: str,
    *,
    token: str | None,
    question_id: str | None = None,
    run_id: str | None = None,
) -> None:
    """Refuse anything the current corpus no longer justifies, before any write.

    Order matters: the token is checked first because a mismatch means the
    caller's whole picture is stale, and reporting a more specific downstream
    reason for a stale picture would invite a client to "fix" the wrong field.
    """
    if action not in ACTIONS:
        raise InterventionError(
            REASON_INVALID_ACTION, f"unknown intervention action {action!r}"
        )
    expected = action_token(facts)
    if not isinstance(token, str) or token != expected:
        raise InterventionError(
            REASON_STALE_ACTION_TOKEN,
            "action token does not match current plan state; refresh the snapshot",
            retryable=True,
            details={"expected_fingerprint": expected},
        )
    if action not in permitted_actions(facts):
        raise InterventionError(
            REASON_ACTION_NOT_PERMITTED,
            disabled_reasons(facts).get(action, f"{action} is not permitted"),
            details={"action": action},
        )
    if run_id is not None and run_id != facts.run_id:
        raise InterventionError(
            REASON_RUN_MISMATCH,
            "request names a run that is no longer current",
            details={"current_run_id": facts.run_id},
        )
    if action in (ACTION_REPLY, ACTION_RESOLVE):
        if facts.question_id is None:
            raise InterventionError(
                REASON_NO_OPEN_QUESTION, "no open question is waiting for a reply"
            )
        if selected_question_id(facts, question_id) is None:
            raise InterventionError(
                REASON_QUESTION_MISMATCH,
                "request names a question that is not open on this plan",
                details={
                    "current_question_id": facts.question_id,
                    "unanswered_ids": list(facts.unanswered_ids),
                },
            )
    if action == ACTION_RESUME and not facts.paused:
        raise InterventionError(REASON_NOT_PAUSED, "run is not paused for a reply")


# --------------------------------------------------------------------------
# Mailbox closure services
# --------------------------------------------------------------------------


def write_supervisor_lease(
    plan_dir: str | Path,
    state_dir: str | Path | None,
    supervisor_id: str | None = None,
) -> mailbox_models.MailboxLease:
    """Heartbeat a live supervisor's lease, or take over when none is alive.

    A fresh, current-version lease that carries a ``run_id`` proves a live
    supervisor is projecting this plan, so re-emit its ``supervisor_id`` and
    question projection verbatim with only ``updated_at`` refreshed — a
    heartbeat, not a takeover. A missing, stale, v1, or unprojected lease
    means no supervisor is running, so the caller legitimately becomes one:
    write ``supervisor_id`` (minted fresh when omitted) with no projection.
    A lease read failure degrades to the takeover path (fail-open, as the
    blind write always was).
    """
    lease: mailbox_models.MailboxLease | None
    try:
        view = mailbox_leases.read_lease(plan_dir, state_dir=state_dir)
    except OSError, mailbox_models.MailboxError:
        lease = None
    else:
        lease = view.lease if view.fresh else None
    if (
        lease is not None
        and lease.version == mailbox_constants.MAILBOX_LEASE_VERSION
        and isinstance(lease.run_id, str)
        and lease.run_id.strip()
    ):
        return mailbox_leases.write_lease(
            plan_dir,
            lease.supervisor_id,
            state_dir=state_dir,
            run_id=lease.run_id,
            projection_seq=lease.projection_seq,
            unanswered_questions=lease.unanswered_questions,
            latest_unanswered_question_id=(lease.latest_unanswered_question_id),
            latest_unanswered_question_created_at=(
                lease.latest_unanswered_question_created_at
            ),
        )
    return mailbox_leases.write_lease(
        plan_dir,
        str(uuid.uuid4()) if supervisor_id is None else supervisor_id,
        state_dir=state_dir,
    )
