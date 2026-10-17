"""Shared fixtures for support asl overseer decisions."""

from __future__ import annotations

from agent_session_loop import (
    clock,
    mailbox_models,
    mailbox_vocabulary,
    overseer_decisions,
    overseer_history,
    overseer_policy_client,
)

PLAN = "/state/agent-session-loop/plan-abc"


OTHER_PLAN = "/state/agent-session-loop/plan-xyz"


PROJECT = "/home/dev/scripts"


OTHER_PROJECT = "/home/dev/api"


RUN = "20260301T090000000000Z-4242"


FINGERPRINT = "fp-0123456789abcdef"


DIGEST = "rd-fedcba9876543210"


EARLY = "2026-03-01T10:00:00+00:00"


LATE = "2026-03-01T12:00:00+00:00"


LATEST = "2026-03-01T14:00:00+00:00"


GRANT = overseer_policy_client.Grant(
    plan_dir=PLAN,
    access=overseer_policy_client.ACCESS_CONTEXT,
    request_digest=DIGEST,
    config_fingerprint=FINGERPRINT,
)


PLAN_SCOPE = overseer_decisions.Scope(project=PROJECT, plan_dir=PLAN, run_id=RUN)


def _excerpt(
    *,
    index: int = 0,
    role: str = "user",
    text: str = "commit the checkpoint and resume",
    timestamp: str = EARLY,
) -> overseer_history.Excerpt:
    return overseer_history.Excerpt(
        index=index,
        role=role,
        timestamp=timestamp,
        text=text,
        truncated=False,
    )


def _conversation(
    *,
    directory: str = PROJECT,
    session_id: str = "sess-1",
    session_class: str = overseer_decisions.SESSION_CLASS_MAIN,
    excerpts: tuple[overseer_history.Excerpt, ...] = (),
) -> overseer_history.Conversation:
    return overseer_history.Conversation(
        harness="claude",
        session_id=session_id,
        profiles=("default",),
        directory=directory,
        projects=("scripts",),
        path=f"{directory}/.transcripts/{session_id}.jsonl",
        started_at=EARLY,
        last_at=LATE,
        session_class=session_class,
        message_count=len(excerpts),
        truncated=False,
        excerpts=excerpts,
    )


def _history(
    *conversations: overseer_history.Conversation,
    allowed: bool = True,
    available: bool = True,
    request_digest: str = DIGEST,
) -> overseer_history.History:
    return overseer_history.History(
        plan_dir=PLAN,
        access=overseer_policy_client.ACCESS_CONTEXT,
        repositories=(),
        limits=overseer_history.Limits(),
        allowed=allowed,
        available=available,
        reason="opted_in" if allowed else "excluded",
        failure=None,
        summary="",
        config_fingerprint=FINGERPRINT,
        request_digest=request_digest,
        contract_version=overseer_history.HISTORY_CONTRACT_VERSION,
        exit_status=0,
        outcomes=(),
        examined=len(conversations),
        truncated=False,
        conversations=conversations,
        withheld=(),
        warnings=(),
    )


def _message(
    *,
    seq: int = 1,
    kind: str = mailbox_vocabulary.KIND_GUIDANCE,
    text: str = "commit the checkpoint and resume",
    created_at: str = EARLY,
) -> mailbox_models.MailboxMessage:
    return mailbox_models.MailboxMessage(
        seq=seq,
        id=f"msg-{seq}",
        kind=kind,
        created_at=created_at,
        text=text,
    )


def _direct_statement(
    *,
    statement_id: str = "transcript:claude:sess-1#0",
    voice: str = "user_instruction",
    scope: overseer_decisions.Scope | None = None,
    at: str = EARLY,
    text: str = "commit the checkpoint and resume",
) -> overseer_decisions.Statement:
    """One statement built directly, for shapes no evidence source produces."""
    return overseer_decisions.Statement(
        statement_id=statement_id,
        voice=voice,
        space=overseer_decisions.TRANSCRIPT_SPACE,
        locator="claude:sess-1",
        scope=scope if scope is not None else overseer_decisions.Scope(project=PROJECT),
        at=at,
        text=text,
        moment=clock.parse_datetime(at),
    )


def _claim(
    statement_id: str,
    *,
    topic: str = "operations",
    stance: str = "allow",
    operations: tuple[str, ...] = ("commit", "resume"),
    scope: overseer_decisions.Scope | None = None,
    corroborating: tuple[str, ...] = (),
    reading: str = "",
) -> overseer_decisions.Claim:
    return overseer_decisions.Claim(
        statement_id=statement_id,
        topic=topic,
        stance=stance,
        operations=operations,
        scope=scope if scope is not None else PLAN_SCOPE,
        corroborating=corroborating,
        reading=reading,
    )
