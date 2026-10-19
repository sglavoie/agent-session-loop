"""Shared fixtures for test_asl_overseer_mailbox."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    intervention,
    mailbox,
    mailbox_appenders,
    mailbox_cursors,
    mailbox_snapshot,
    overseer_decisions,
    overseer_dispatch,
    overseer_mailbox,
    overseer_policy_client,
    overseer_store_ledger,
)

PROJECT = "/home/dev/scripts"
RUN = "run-1"
FINGERPRINT = "fp-first"
MOMENT = "2026-01-01T00:00:00+00:00"


def _ruling(
    *,
    plan_dir: str,
    incident_id: int,
    operation: str = "reply",
    run_id: str = RUN,
    project: str = PROJECT,
    decision_id: str = "d-1",
    stance: str = "allow",
    at: str = MOMENT,
    authorized: bool = True,
    refusal: str | None = None,
    with_decision: bool = True,
    settlements: tuple[overseer_decisions.Settlement, ...] = (),
) -> overseer_decisions.Ruling:
    """A ruling that authorizes one operation on one plan, run and incident."""
    scope = overseer_decisions.Scope(
        project=project,
        plan_dir=plan_dir,
        run_id=run_id,
        incident=str(incident_id),
    )
    decision = overseer_decisions.Decision(
        decision_id=decision_id,
        statement_id="s-1",
        topic="operations",
        stance=stance,  # type: ignore[arg-type]
        scope=scope,
        operations=(operation,),  # type: ignore[arg-type]
        at=at,
        moment=datetime.fromisoformat(at),
        sources=("s-1",),
        text="answer the question",
    )
    return overseer_decisions.Ruling(
        operation=operation,
        scope=scope,
        authorized=authorized,
        refusal=refusal,  # type: ignore[arg-type]
        decision=decision if with_decision else None,
        settlements=settlements,
    )


class _MailboxCase(unittest.TestCase):
    """A real state root, a real ledger, and one plan with one open question."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="asl-overseer-mailbox-"))
        self.addCleanup(self._close)
        self.ledger = overseer_store_ledger.open_ledger(state_dir=self.root)
        self.ledger.note_policy(config_fingerprint=FINGERPRINT)
        self.plan = self.new_plan("plan-a")
        self.incident_id = self.ledger.open_incident(
            grant=None,
            plan_dir=self.plan,
            run_id=RUN,
            kind="question_pending",
            summary="waiting on a reply",
        ).incident_id
        self.question = mailbox_appenders.append_question(
            self.plan,
            "may I proceed?",
            state_dir=self.root,
            run_id=RUN,
        ).id

    def _close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self.ledger.close()

    def new_plan(self, name: str) -> str:
        """A real plan directory, canonical, so every read agrees on its name."""
        path = self.root / "plans" / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def grant(self, plan_dir: str) -> overseer_policy_client.Grant:
        return overseer_policy_client.Grant(
            plan_dir=plan_dir,
            access=overseer_policy_client.ACCESS_ACTION,
            request_digest="rd-1",
            config_fingerprint=FINGERPRINT,
        )

    def break_mailbox(self, plan_dir: str) -> None:
        """Leave a directory where the mailbox log belongs: unreadable, durably."""
        path = mailbox_cursors.mailbox_path_for_plan(plan_dir, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        path.mkdir()

    def view(self, plan_dir: str | None = None) -> mailbox_snapshot.MailboxView:
        return mailbox.read_mailbox(plan_dir or self.plan, state_dir=self.root)

    def kinds(self, plan_dir: str | None = None) -> list[str]:
        return [message.kind for message in self.view(plan_dir).messages]

    def ruling(self, **kwargs: Any) -> overseer_decisions.Ruling:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
        }
        arguments.update(kwargs)
        return _ruling(**arguments)

    def post_reply(self, **kwargs: Any) -> overseer_mailbox.Posting:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "question_id": self.question,
            "text": "carry on",
            "ruling": self.ruling(),
            "run_id": RUN,
            "state_dir": self.root,
        }
        arguments.update(kwargs)
        return overseer_mailbox.post_reply(**arguments)

    def post_guidance(self, **kwargs: Any) -> overseer_mailbox.Posting:
        arguments: dict[str, Any] = {
            "plan_dir": self.plan,
            "incident_id": self.incident_id,
            "text": "prefer the smaller change",
            "ruling": self.ruling(operation="guidance"),
            "run_id": RUN,
            "state_dir": self.root,
        }
        arguments.update(kwargs)
        return overseer_mailbox.post_guidance(**arguments)

    def action(
        self,
        payload: overseer_dispatch.Payload,
        *,
        plan_dir: str | None = None,
    ) -> overseer_dispatch.Action:
        return overseer_dispatch.Action(
            plan_dir=plan_dir or self.plan,
            run_id=RUN,
            incident_id=self.incident_id,
            payload=payload,
            token="token-1",
            scope="sha256:scope",
            repositories=(),
        )

    def facts(self, *, unanswered: tuple[str, ...] | None = None) -> Any:
        ids = (self.question,) if unanswered is None else unanswered
        return intervention.RunFacts(
            plan_dir=Path(self.plan),
            run_id=RUN,
            lock_active=True,
            run_active=True,
            paused=True,
            turn_active=False,
            context_kind="waiting",
            question_id=ids[0] if ids else None,
            unanswered_ids=ids,
        )


def _state(posting: overseer_mailbox.Posting | None) -> str:
    assert posting is not None
    return posting.state


def _stale_first_read(
    case: _MailboxCase,
    stale: mailbox_snapshot.MailboxView,
) -> Any:
    """Serve one stale snapshot, then the mailbox as it actually stands.

    This is the race the module cannot prevent and has to survive: the world
    moved between the read that checked and the append that acted.
    """
    real_read = mailbox.read_mailbox
    served = {"count": 0}

    def reader(*args: Any, **kwargs: Any) -> mailbox_snapshot.MailboxView:
        served["count"] += 1
        if served["count"] == 1:
            return stale
        return real_read(*args, **kwargs)

    return mock.patch.object(mailbox, "read_mailbox", side_effect=reader)
