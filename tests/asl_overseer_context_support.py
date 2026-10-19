# ruff: noqa: F401
"""Evidence packets: what they describe, what they refuse, and how little they hold.

Five claims, none of which is provable by reading ``overseer_context``.

*References, not copies.* The ledger is a durable file and project content put
into it outlives every authorization. ``EvidenceIsReferencedNotStoredTests``
writes a marker into a question and a commit path and then greps the ledger's
own bytes for it, which is the only way to prove the content never landed there.

*Current, not filed.* A packet's summary is regenerated on every load, so a
question answered after the incident was opened has to disappear from the
packet and the mailbox has to come back as ``changed``. Asserted by answering
the question between a build and a load over the same incident.

*Bounded.* A mailbox and a run's turn artifacts are unbounded in principle. The
fixture writes fifty questions and a fifty-thousand-character guidance and
requires the summary, the identities and the excerpt to stay inside their
published caps -- and requires the overrun to be *reported* through
``truncated`` rather than silently dropped.

*Scope.* A packet is authorized against a repository set and a run. Moving a
declared root, replacing the run, and removing the record entirely each have to
produce a distinct refusal, and the refusal has to happen before anything is
read -- the mailbox seam counts its calls, so "refused without reading" is an
assertion rather than a hope.

*Exclusion.* Revocation and a policy change both have to make an already-built
packet unloadable and unexpandable. The policy change is recoverable by an
explicit release; the revocation is not, and ``PermanentExclusionTests`` proves
the release itself refuses rather than that nobody calls it.

Nothing here spawns a process: the policy client is never reached, because a
``Grant`` is a value and these tests construct the ones AgentMux would have
returned. ``test_assembling_context_spawns_no_process`` asserts that rather
than assuming it.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, get_args
from unittest import mock

from agent_session_loop import (
    clock,
    events,
    mailbox,
    mailbox_appenders,
    mailbox_models,
    mailbox_snapshot,
    overseer_context,
    overseer_discovery,
    overseer_policy_client,
    overseer_store_ledger,
    overseer_store_models,
    plans_files,
    state,
)

from .support import assert_no_runner_import

#: The fixed moment the fixture stamps everything from. Far enough from any
#: real "now" that a test accidentally reading the wall clock fails loudly.
EPOCH = datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC)

#: The configuration fingerprint AgentMux is pretending to publish.
FINGERPRINT = "fp-1"

#: A string that appears in project content and must never appear in the
#: ledger. Distinctive enough that finding it in a database file is proof.
MARKER = "zzz-secret-payload-zzz"


def question_uuid(index: int) -> str:
    """A stable, parseable message identifier.

    The mailbox rejects anything that is not a UUID, and a fixture that lets it
    mint its own identifiers cannot then assert which ones a packet named.
    """
    return f"00000000-0000-4000-8000-{index:012d}"


#: The question the fixture's default run is waiting on.
QUESTION = question_uuid(1)


class _Plan:
    """One run's published records, a real ledger, and the grant over both.

    Everything is written the way the runner writes it -- a ``state.json`` in
    the plan's state directory, turn and commit artifacts under
    ``runs/<run_id>/`` -- because a projection tested against a hand-built dict
    proves only that the dict matches itself.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans_files.canonicalize_path(tmp.name)
        self.state_root = self.root / "state"
        self.plan_dir = self.root / "plans" / "plan-alpha"
        self.plan_dir.mkdir(parents=True)
        self.plan_dir = plans_files.canonicalize_path(self.plan_dir)
        self.run_id = "run-1"
        self.now = EPOCH
        self.ledger = overseer_store_ledger.open_ledger(
            state_dir=self.state_root, now=self.iso_now
        )
        case.addCleanup(self.ledger.close)
        self.mailbox_reads = 0
        self.write_state()

    # -- clock -------------------------------------------------------------

    def iso_now(self) -> str:
        return clock.isoformat(self.now)

    # -- corpus ------------------------------------------------------------

    @property
    def plan_state(self) -> Path:
        return state.plan_state_dir(self.plan_dir, self.state_root)

    def write_state(self, **overrides: Any) -> dict[str, Any]:
        """Write the run's published record, paused and waiting by default."""
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "harness": "claude",
            "ok": True,
            "paused": True,
            "pause_reason": "blocked",
            "dry_run": False,
            "plan_dir": str(self.plan_dir),
            "cwd": str(self.root),
            "run_started_at": clock.isoformat(EPOCH - timedelta(hours=2)),
            "run_finished_at": None,
            "workspace_root": str(self.root),
            "registry_digest": "d" * 64,
            "repositories": [
                {
                    "alias": "scripts",
                    "path": "scripts",
                    "origin": "github.com/sglavoie/scripts",
                    "resolved_root": str(self.root / "scripts"),
                }
            ],
            "run_config": {"review_harness": "codex", "supervisor_audit": True},
            "active_turn": {"session_id": "8", "turn_index": 3},
            "waiting_for_reply": {"question_id": QUESTION},
            overseer_discovery.PAUSED_AT_KEY: clock.isoformat(
                EPOCH - timedelta(minutes=1)
            ),
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        self.plan_state.mkdir(parents=True, exist_ok=True)
        (self.plan_state / events.STATE_FILE_NAME).write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
        return record

    def write_turn(self, index: int, **payload: Any) -> Path:
        directory = (
            self.plan_state / state.RUNS_DIR_NAME / self.run_id / f"turn-{index:04d}-8"
        )
        directory.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "turn_index": index,
            "session_id": "8",
            "returncode": 0,
            "status_changes": ["8: pending -> complete"],
        }
        record.update(payload)
        path = directory / state.RESULT_FILE_NAME
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return path

    def write_commit(self, index: int, **payload: Any) -> Path:
        directory = (
            self.plan_state
            / state.RUNS_DIR_NAME
            / self.run_id
            / f"commit-{index:04d}-8"
        )
        directory.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "commit_index": index,
            "scope": "session",
            "session_id": "8",
            "repository_alias": "scripts",
            "qualified_paths": [f"scripts:agent_session_loop/{MARKER}.py"],
            "status_result": "success",
            "commit_sha": "a" * 40,
            "head_before": "b" * 40,
            "head_after": "a" * 40,
            "clean_after": True,
        }
        record.update(payload)
        path = directory / state.RESULT_FILE_NAME
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        return path

    def ask(self, text: str, *, question_id: str) -> mailbox_models.MailboxMessage:
        return mailbox_appenders.append_question(
            self.plan_dir,
            text,
            state_dir=self.state_root,
            message_id=question_id,
            run_id=self.run_id,
            session_id="8",
            turn_index=3,
        )

    def answer(self, question_id: str) -> None:
        mailbox_appenders.append_reply(
            self.plan_dir, question_id, "go ahead", state_dir=self.state_root
        )

    def guide(self, text: str) -> mailbox_models.MailboxMessage:
        return mailbox_appenders.append_guidance(
            self.plan_dir, text, state_dir=self.state_root
        )

    def populate(self) -> None:
        """Every family, so a packet has all six sources to describe."""
        self.ask(f"which way? {MARKER}", question_id=QUESTION)
        self.guide("prefer the smaller change")
        self.write_turn(
            1,
            verification={
                "session": "8",
                "passed": False,
                "reason": "failed",
                "checks": [{"name": "unittest", "passed": False}],
                "failures": [{"name": "unittest", "passed": False}],
            },
        )
        self.write_commit(1)

    # -- authorization -----------------------------------------------------

    def grant(
        self,
        *,
        plan_dir: str | None = None,
        access: str = overseer_context.CONTEXT_ACCESS,
        fingerprint: str = FINGERPRINT,
    ) -> overseer_policy_client.Grant:
        """The grant AgentMux would have returned for a permitted plan."""
        return overseer_policy_client.Grant(
            plan_dir=plan_dir if plan_dir is not None else str(self.plan_dir),
            access=access,
            request_digest="rd-1",
            config_fingerprint=fingerprint,
        )

    def candidate(self) -> overseer_discovery.Candidate:
        found = self._candidate()
        assert found is not None, "the fixture's own plan is not discoverable"
        return found

    def _candidate(self) -> overseer_discovery.Candidate | None:
        inventory = overseer_discovery.build_inventory(state_dir=self.state_root)
        for candidate in inventory.candidates:
            if candidate.plan_dir == str(self.plan_dir):
                return candidate
        return None

    # -- driving -----------------------------------------------------------

    def read_mailbox(
        self, plan_dir: str | Path, *, state_dir: str | Path | None = None
    ) -> mailbox_snapshot.MailboxView:
        """The mailbox seam, counted, so "refused without reading" is provable."""
        self.mailbox_reads += 1
        return mailbox.read_mailbox(plan_dir, state_dir=state_dir)

    def build(self, **overrides: Any) -> overseer_context.Packet:
        kwargs: dict[str, Any] = {
            "grant": self.grant(),
            "ledger": self.ledger,
            "candidate": self.candidate(),
            "kind": "blocked",
            "summary": "the run is waiting on a human",
            "state_dir": self.state_root,
            "now": self.iso_now,
            "read_mailbox": self.read_mailbox,
        }
        kwargs.update(overrides)
        return overseer_context.build_packet(**kwargs)

    def load(self, incident_id: int, **overrides: Any) -> overseer_context.Packet:
        kwargs: dict[str, Any] = {
            "grant": self.grant(),
            "ledger": self.ledger,
            "incident_id": incident_id,
            "state_dir": self.state_root,
            "now": self.iso_now,
            "read_mailbox": self.read_mailbox,
        }
        kwargs.update(overrides)
        return overseer_context.load_packet(**kwargs)

    def expand(
        self, incident_id: int, kind: overseer_context.SourceKind, **overrides: Any
    ) -> overseer_context.Excerpt:
        kwargs: dict[str, Any] = {
            "grant": self.grant(),
            "ledger": self.ledger,
            "incident_id": incident_id,
            "kind": kind,
            "state_dir": self.state_root,
            "read_mailbox": self.read_mailbox,
        }
        kwargs.update(overrides)
        return overseer_context.expand(**kwargs)


def _kinds(packet: overseer_context.Packet) -> set[str]:
    return {source.kind for source in packet.sources}


def _gaps(packet: overseer_context.Packet) -> dict[str, str]:
    return {gap.kind: gap.reason for gap in packet.gaps}


__all__ = [
    "FINGERPRINT",
    "MARKER",
    "QUESTION",
    "_Plan",
    "_gaps",
    "_kinds",
    "assert_no_runner_import",
    "events",
    "get_args",
    "json",
    "mock",
    "overseer_context",
    "overseer_policy_client",
    "overseer_store_ledger",
    "overseer_store_models",
    "question_uuid",
    "subprocess",
    "unittest",
]
