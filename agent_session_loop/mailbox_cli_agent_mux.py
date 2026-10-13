"""agent-mux notifications for mailbox questions, replies and resolutions."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any

from . import (
    agent_mux_delivery,
    cli_common,
    mailbox,
    mailbox_cli_models,
    mailbox_models,
    mailbox_vocabulary,
)


def _kitty_window_id() -> int | None:
    raw = os.environ.get("KITTY_WINDOW_ID")
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        return None
    return value if value > 0 else None


def notify_agent_mux_mailbox_reply(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    message: mailbox_models.MailboxMessage,
    projection: mailbox_cli_models.MailboxReplyProjection | None,
) -> dict[str, Any] | None:
    """Best-effort fast clear after a reply authoritatively empties the outbox."""
    if projection is None or message.to != projection.question_id:
        return None
    try:
        post_reply = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
        if post_reply.last_seq < message.seq or post_reply.open_questions(
            projection.run_id
        ):
            return None

        runner_state = context.status.get("runner_state")
        harness = (
            runner_state.get("harness")
            if isinstance(runner_state, dict)
            and isinstance(runner_state.get("harness"), str)
            else None
        )
        payload: dict[str, Any] = {
            "run_id": projection.run_id,
            "plan_dir": str(context.snapshot.plan_dir),
            "cwd": os.getcwd(),
            "state_path": context.status.get("state_path"),
            "harness": harness,
        }
        kitty_window_id = _kitty_window_id()
        if kitty_window_id is not None:
            payload["kitty_window_id"] = kitty_window_id
        return agent_mux_delivery.notify(
            agent_mux_delivery.MAILBOX_REPLY_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_REPLY_REASON,
            active_turn=None,
            extra={
                "question_id": projection.question_id,
                "delivery_state": "accepted",
                "consumed": False,
                # A lease that predates the question carries a stale
                # generation; the reply's seq is monotonic in the same file,
                # so it substitutes as a valid generation counter.
                "mailbox_projection_sequence": str(
                    message.seq
                    if projection.lease_predates_question
                    else projection.projection_seq
                ),
                "source_event_id": (f"{projection.run_id}:mailbox-reply:{message.seq}"),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: message.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - mailbox replies never depend on AgentMux
        return None


def notify_agent_mux_mailbox_resolution(
    context: cli_common.PlanCommandContext,
    args: argparse.Namespace,
    message: mailbox_models.MailboxMessage,
    projection: mailbox_cli_models.MailboxReplyProjection | None,
    *,
    active_run_id: str,
) -> dict[str, Any] | None:
    """Best-effort fast clear after an authoritative answerless closure."""
    if (
        projection is None
        or projection.run_id != active_run_id
        or message.to != projection.question_id
        or message.kind != mailbox_vocabulary.KIND_RESOLUTION
        or message.action not in mailbox_vocabulary.RESOLUTION_ACTIONS
    ):
        return None
    try:
        post_resolution = mailbox.read_mailbox(
            context.snapshot.plan_dir,
            state_dir=args.state_dir,
        )
        if post_resolution.last_seq < message.seq or post_resolution.open_questions(
            projection.run_id
        ):
            return None

        runner_state = context.status.get("runner_state")
        harness = (
            runner_state.get("harness")
            if isinstance(runner_state, dict)
            and isinstance(runner_state.get("harness"), str)
            else None
        )
        payload: dict[str, Any] = {
            "run_id": projection.run_id,
            "plan_dir": str(context.snapshot.plan_dir),
            "cwd": os.getcwd(),
            "state_path": context.status.get("state_path"),
            "harness": harness,
        }
        kitty_window_id = _kitty_window_id()
        if kitty_window_id is not None:
            payload["kitty_window_id"] = kitty_window_id
        return agent_mux_delivery.notify(
            agent_mux_delivery.MAILBOX_RESOLUTION_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_RESOLUTION_REASON,
            active_turn=None,
            extra={
                "question_id": projection.question_id,
                "resolution_action": message.action,
                "delivery_state": "accepted",
                "consumed": False,
                "mailbox_projection_sequence": str(
                    message.seq
                    if projection.lease_predates_question
                    else projection.projection_seq
                ),
                "source_event_id": (
                    f"{projection.run_id}:mailbox-resolution:{message.seq}"
                ),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: message.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - resolutions never depend on AgentMux
        return None


def notify_agent_mux_mailbox_question(
    context: cli_common.PlanCommandContext,
    projection: mailbox_cli_models.MailboxQuestionProjection | None,
) -> dict[str, Any] | None:
    """Best-effort push notification for one surfaced actionable open set."""
    if projection is None:
        return None
    try:
        runner_state = context.status.get("runner_state")
        harness = (
            runner_state.get("harness")
            if isinstance(runner_state, dict)
            and isinstance(runner_state.get("harness"), str)
            else None
        )
        payload: dict[str, Any] = {
            "run_id": projection.run_id,
            "plan_dir": str(context.snapshot.plan_dir),
            "cwd": os.getcwd(),
            "state_path": context.status.get("state_path"),
            "harness": harness,
        }
        kitty_window_id = _kitty_window_id()
        if kitty_window_id is not None:
            payload["kitty_window_id"] = kitty_window_id
        latest = projection.latest_question
        return agent_mux_delivery.notify(
            agent_mux_delivery.MAILBOX_QUESTION_EVENT,
            payload,
            reason=agent_mux_delivery.MAILBOX_QUESTION_REASON,
            active_turn=None,
            extra={
                "question_id": latest.id,
                "question_created_at": latest.created_at,
                "unanswered_questions": len(projection.open_questions),
                "delivery_state": "open",
                "mailbox_projection_sequence": str(projection.projection_seq),
                "source_event_id": (
                    f"{projection.run_id}:mailbox-question:{latest.seq}"
                ),
            },
            enabled=agent_mux_delivery.notify_enabled(),
            timeout=agent_mux_delivery.notify_timeout(),
            allocate_sequence=lambda: latest.seq,
            record_outcome=lambda _outcome: None,
            resolve_binary=shutil.which,
            run_command=subprocess.run,
            emitted_at=lambda: datetime.now(UTC),
        )
    except Exception:  # noqa: BLE001 - outbox delivery never depends on AgentMux
        return None
