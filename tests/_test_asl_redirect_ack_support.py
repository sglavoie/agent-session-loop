# mypy: ignore-errors
# ruff: noqa
"""Which corrections a resumed prompt carries, and what counts as receipt.

Session 11 delivered an accepted correction to the next attempt of the same
session. These tests are about the two things it left undecided, and both are
about *losing* something -- a correction the operator sent, or the evidence that
a worker read one.

The claims, in the order the module argues them:

* the boundary is a moment rather than a filter: everything unsettled against an
  earlier attempt of this session is swept into the prompt being finalized,
  merged with what the gate accepted mid-turn and ordered by submission, while
  anything aimed at the attempt about to start is left to interrupt it in turn;
* every carried message survives that merge, and a refusal never costs the
  corrections beside it;
* carrying is a write that happens once -- a re-prepared prompt re-reads the
  same set instead of sweeping up whatever arrived since, and no annotation
  moves a request out of ``accepted``, so the journal's "settled once"
  invariant is untouched;
* carrying is also not yet delivering: an attempt that binds a correction into
  a prompt and then never launches gives it back, an attempt that launched may
  not, and the difference is recorded rather than inferred -- which is what
  keeps a pause between the two from spending the operator's words;
* a receipt needs both halves of its identity, so the two cheap impostors --
  the stale worker still alive from the interrupted turn, and a transcript
  replaying an older prompt -- are refused, as is a worker naming an attempt
  the journal never recorded as the carrier;
* a receipt is idempotent, and a hand-written one is refused on replay too,
  because the journal is the only evidence and a file is editable;
* and the wait is observational: a timeout writes nothing, retracts nothing,
  and reports no failure.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

from agent_session_loop import (
    clock,
    mailbox_cli_args,
    mailbox_cli_inbox,
    redirect_ack,
    redirect_ack_annotate,
    redirect_ack_attempt,
    redirect_ack_carry,
    redirect_continuation,
    redirect_store_carry,
    redirect_store_journal,
    redirect_store_models,
    redirect_store_requests,
    redirect_store_validation,
    redirect_store_vocab,
    responses,
    run_loop_carried_corrections,
)

from .support import write_plan
from .support_asl_turn_dispositions import RUN_ID, FakeRunContext

SESSION_ID = "1"

#: A caller that passed nothing, which is not the same as one that passed
#: ``None`` -- "no live attempt" is one of the cases under test.
_DEFAULT = object()

FIRST = "stop editing README and re-read the contract"
SECOND = "the parser you picked cannot see the second column"
THIRD = "and leave the fixture alone"


def target(
    turn_index: int = 1,
    *,
    harness: str | None = "claude",
    run_id: str = RUN_ID,
    session_id: str = SESSION_ID,
) -> redirect_store_models.RedirectTarget:
    return redirect_store_models.RedirectTarget(
        run_id=run_id,
        session_id=session_id,
        turn_index=turn_index,
        harness=harness,
    )


class _FakeCommandContext:
    """Only the member ``inbox acknowledge`` reads off a resolved plan."""

    def __init__(self, plan_dir: Path) -> None:
        self.snapshot = type("_Snapshot", (), {"plan_dir": plan_dir})()


class RedirectAckTestCase(unittest.TestCase):
    """A plan with its own state root, driving the real journal file."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.plan_dir = self.root / "plan"
        self.state_dir = self.root / "state"
        write_plan(self.plan_dir, ("pending",))

    def submit(
        self,
        feedback: str = FIRST,
        *,
        turn_index: int = 1,
        accept: bool = False,
        **kwargs: Any,
    ) -> redirect_store_models.RedirectRequest:
        request = redirect_store_requests.submit_request(
            self.plan_dir,
            target=target(turn_index),
            feedback=feedback,
            state_dir=self.state_dir,
            **kwargs,
        )
        if accept:
            return redirect_store_requests.accept_request(
                self.plan_dir,
                request.operation_id,
                target=target(turn_index),
                state_dir=self.state_dir,
            )
        return request

    def carry(
        self,
        *,
        continuation: redirect_store_models.RedirectTarget | None = None,
        accepted: tuple[str, ...] = (),
    ) -> redirect_ack_carry.CarriedCorrections:
        return redirect_ack_carry.carry_corrections(
            self.plan_dir,
            continuation=continuation or target(2, harness="codex"),
            accepted_operation_ids=accepted,
            state_dir=self.state_dir,
        )

    def read(self) -> redirect_store_models.RedirectJournalView:
        return redirect_store_journal.read_journal(
            self.plan_dir, state_dir=self.state_dir
        )

    def journal_path(self) -> Path:
        return redirect_store_journal.journal_path_for_plan(
            self.plan_dir, self.state_dir
        )

    def journal_lines(self) -> list[dict[str, Any]]:
        path = self.journal_path()
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def append_raw(self, payload: dict[str, Any]) -> None:
        with self.journal_path().open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")


__all__ = [name for name in globals() if not name.startswith("__")]
