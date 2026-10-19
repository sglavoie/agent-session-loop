# mypy: disable-error-code="attr-defined,call-arg,has-type,name-defined"
# ruff: noqa: F401, F403, F405, F821, F822, I001
"""Question reply and resolution records for ASL interventions."""

from __future__ import annotations

from . import intervention_actions as _actions
from . import intervention_facts as _facts

globals().update(
    {name: value for name, value in vars(_facts).items() if not name.startswith("__")}
)
globals().update(
    {name: value for name, value in vars(_actions).items() if not name.startswith("__")}
)
del _actions, _facts


@dataclass(frozen=True)
class ClosureOutcome:
    """One durable mailbox closure, described without its text."""

    message: mailbox_models.MailboxMessage
    supervisor_id: str
    mailbox_path: Path
    lease_path: Path

    @property
    def closure_id(self) -> str:
        return self.message.id


def _closure(
    plan_dir: Path,
    state_dir: str | Path | None,
    append: Any,
) -> ClosureOutcome:
    # The lease lands first so an unwritable state root fails before a closure
    # the caller may legitimately retry has already been appended.
    lease = write_supervisor_lease(plan_dir, state_dir)
    message = append()
    return ClosureOutcome(
        message=message,
        supervisor_id=lease.supervisor_id,
        mailbox_path=mailbox_cursors.mailbox_path_for_plan(plan_dir, state_dir),
        lease_path=mailbox_cursors.lease_path_for_plan(plan_dir, state_dir),
    )


def append_question_reply(
    plan_dir: str | Path,
    question_id: str,
    text: str,
    *,
    state_dir: str | Path | None = None,
) -> ClosureOutcome:
    """Append the first supervisor reply to a live question."""
    canonical = _canonical_plan_dir(plan_dir)
    if not isinstance(text, str) or not text.strip():
        raise InterventionError(
            REASON_EMPTY_TEXT, "reply text must contain non-whitespace content"
        )
    return _closure(
        canonical,
        state_dir,
        lambda: mailbox_appenders.append_reply(
            canonical, question_id, text, state_dir=state_dir
        ),
    )


def append_question_resolution(
    plan_dir: str | Path,
    question_id: str,
    action: str,
    *,
    expected_run_id: str,
    note: str | None = None,
    state_dir: str | Path | None = None,
) -> ClosureOutcome:
    """Append the first answerless closure for a live question."""
    canonical = _canonical_plan_dir(plan_dir)
    if action not in mailbox_vocabulary.RESOLUTION_ACTIONS:
        raise InterventionError(
            mailbox_constants.REJECT_INVALID_ACTION,
            "resolution action must be either continue or pause",
            details={"action": repr(action)},
        )
    return _closure(
        canonical,
        state_dir,
        lambda: mailbox_appenders.append_resolution(
            canonical,
            question_id,
            action,
            expected_run_id=expected_run_id,
            note=note,
            state_dir=state_dir,
        ),
    )


def closure_receipt(
    plan_dir: str | Path,
    closure_id: str,
    *,
    state_dir: str | Path | None = None,
) -> mailbox_models.MailboxMessage | None:
    """Return the consumer's receipt for one closure, if it has landed."""
    try:
        view = mailbox.read_mailbox(plan_dir, state_dir=state_dir)
    except OSError, ValueError, mailbox_models.MailboxError:
        return None
    return view.receipt_for_closure(closure_id)


def question_body(
    plan_dir: str | Path,
    *,
    question_id: str | None = None,
    state_dir: str | Path | None = None,
) -> tuple[str, str, str]:
    """Return ``(question_id, context_kind, body)`` for one selected question.

    A live wait resolves to the mailbox record an operator is answering. A
    paused run has no mailbox question at all, so the synthetic one is the
    harness's own last message, which is exactly what the Q&A resolver reads.
    """
    canonical = _canonical_plan_dir(plan_dir)
    facts = inspect_run(canonical, state_dir=state_dir)
    selected = question_id or facts.question_id

    if selected is not None:
        try:
            unanswered = mailbox.unanswered_questions_for_run(
                canonical, facts.run_id, state_dir=state_dir
            )
        except (OSError, ValueError, mailbox_models.MailboxError) as error:
            raise InterventionError(
                REASON_MISSING_QUESTION_BODY,
                f"cannot read the mailbox for {canonical}: {error}",
                retryable=True,
            ) from error
        for message in unanswered:
            if message.id == selected:
                if not isinstance(message.text, str) or not message.text:
                    raise InterventionError(
                        REASON_MISSING_QUESTION_BODY,
                        f"question {selected} carries no text",
                    )
                return selected, "waiting", message.text
        if question_id is not None:
            raise InterventionError(
                REASON_QUESTION_MISMATCH,
                f"question {question_id} is not open on this plan",
                details={"current_question_id": facts.question_id},
            )

    if not facts.paused:
        raise InterventionError(
            REASON_NO_OPEN_QUESTION, "no open question is waiting for a reply"
        )
    try:
        context = qna.load_qna_context(canonical, state_dir=state_dir, reclaim=False)
    except qna.QnAError as error:
        raise InterventionError(
            REASON_MISSING_QUESTION_BODY,
            f"cannot load the paused question context: {error}",
            details={"qna_reason": error.reason},
        ) from error
    synthetic_id = context.question_id or f"pause:{facts.run_id or 'unknown'}"
    return synthetic_id, "pause", context.last_message


def enforce_body_bound(body: str) -> int:
    """Return the encoded size of a body, or refuse one the frame cannot carry."""
    size = len(body.encode("utf-8"))
    if size > MAX_QUESTION_BODY_BYTES:
        raise InterventionError(
            REASON_BODY_TOO_LARGE,
            "question body exceeds the protocol body bound; open the Workbench "
            "to read it",
            details={
                "body_bytes": size,
                "max_body_bytes": MAX_QUESTION_BODY_BYTES,
                "fallback": ACTION_WORKBENCH,
            },
        )
    return size
