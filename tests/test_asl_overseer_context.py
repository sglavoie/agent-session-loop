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


class PacketDescribesTheIncidentTests(unittest.TestCase):
    """A packet names every family the run published, and identifies the run."""

    def test_a_populated_run_yields_every_evidence_family(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        self.assertIsNone(packet.refusal)
        self.assertEqual(set(overseer_context.SOURCE_KINDS), _kinds(packet))
        self.assertEqual({}, _gaps(packet))
        self.assertTrue(packet.complete)

    def test_a_packet_carries_the_identities_that_scope_it(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        self.assertEqual(str(plan.plan_dir), packet.plan_dir)
        self.assertEqual(plan.run_id, packet.run_id)
        self.assertEqual((QUESTION,), packet.questions)
        self.assertEqual("rd-1", packet.request_digest)
        self.assertEqual(FINGERPRINT, packet.config_fingerprint)
        self.assertEqual(overseer_context.scope_digest(plan.candidate()), packet.scope)

    def test_every_source_names_where_it_came_from(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        for source in packet.sources:
            with self.subTest(kind=source.kind):
                self.assertEqual(overseer_context.STATE_SPACE, source.space)
                self.assertTrue(source.digest.startswith("sha256:"))
                self.assertTrue((plan.plan_state / source.locator).is_file())
                self.assertFalse(source.changed)

    def test_the_status_summary_names_the_pause_and_what_it_waits_on(self) -> None:
        plan = _Plan(self)
        plan.populate()

        summary = plan.build().source("status")

        assert summary is not None
        self.assertIn("paused (blocked)", summary.summary)
        self.assertIn("waiting for a reply", summary.summary)

    def test_a_run_with_no_artifacts_reports_gaps_rather_than_five_sources(
        self,
    ) -> None:
        """An absent family is a fact about the run, not a shorter packet."""
        plan = _Plan(self)

        packet = plan.build()

        self.assertEqual({"status", "run_config"}, _kinds(packet))
        self.assertEqual(
            {
                "questions": "absent",
                "verification": "absent",
                "worker_changes": "absent",
                "supervision": "absent",
            },
            _gaps(packet),
        )
        self.assertFalse(packet.complete)

    def test_a_mailbox_with_no_open_question_is_empty_rather_than_absent(self) -> None:
        plan = _Plan(self)
        plan.ask("which way?", question_id=QUESTION)
        plan.answer(QUESTION)

        packet = plan.build()

        self.assertEqual("empty", _gaps(packet)["questions"])
        self.assertEqual((), packet.questions)

    def test_a_run_that_has_not_committed_falls_back_to_its_status_changes(
        self,
    ) -> None:
        plan = _Plan(self)
        plan.write_turn(1)

        source = plan.build().source("worker_changes")

        assert source is not None
        self.assertIn("turn-0001", source.locator)
        self.assertIn("8: pending -> complete", source.summary)

    def test_a_commit_wins_over_a_turn_for_worker_changes(self) -> None:
        plan = _Plan(self)
        plan.write_turn(1)
        plan.write_commit(1)

        source = plan.build().source("worker_changes")

        assert source is not None
        self.assertIn("commit-0001", source.locator)
        self.assertIn("a" * 40, source.summary)

    def test_verification_is_read_from_the_last_turn_that_recorded_one(self) -> None:
        """A turn that paused before verifying does not erase the last verdict."""
        plan = _Plan(self)
        plan.write_turn(
            1, verification={"session": "7", "passed": True, "reason": "passed"}
        )
        plan.write_turn(2)

        source = plan.build().source("verification")

        assert source is not None
        self.assertIn("turn-0001", source.locator)
        self.assertIn("verification of 7", source.summary)

    def test_an_incident_is_opened_with_one_evidence_row_per_source(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        view = plan.ledger.read_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant()
        )
        self.assertEqual(
            {(source.kind, source.locator) for source in packet.sources},
            {
                (row.kind, row.locator)
                for row in view.records
                if row.kind != overseer_context.SCOPE_KIND
            },
        )
        self.assertEqual(
            [packet.incident_id],
            [row.incident_id for row in view.records][:1],
        )

    def test_the_authorized_scope_is_recorded_alongside_the_sources(self) -> None:
        """The repository set is not a file, so nothing else could recover it."""
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        view = plan.ledger.read_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant()
        )
        scope_rows = [
            row for row in view.records if row.kind == overseer_context.SCOPE_KIND
        ]
        self.assertEqual([packet.scope], [row.digest for row in scope_rows])
        self.assertNotIn(overseer_context.SCOPE_KIND, _kinds(packet))
        self.assertNotIn(overseer_context.SCOPE_KIND, set(_gaps(packet)))


class EvidenceIsReferencedNotStoredTests(unittest.TestCase):
    """The ledger holds locators and digests. Content stays where it was."""

    def test_no_source_content_reaches_the_ledger_file(self) -> None:
        plan = _Plan(self)
        plan.populate()

        plan.build()

        plan.ledger.close()
        blob = overseer_store_ledger.ledger_path(plan.state_root).read_bytes()
        self.assertNotIn(MARKER.encode("utf-8"), blob)

    def test_the_digest_is_the_digest_of_the_source_as_it_was_read(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build()

        source = packet.source("status")
        assert source is not None
        path = plan.plan_state / source.locator
        self.assertEqual(path.stat().st_size, source.size)
        self.assertEqual(
            source.digest,
            plan.load(packet.incident_id).source("status").digest,  # type: ignore[union-attr]
        )


class PacketsDescribeTheIncidentNowTests(unittest.TestCase):
    """A loaded packet re-reads. It never replays what it was told once."""

    def test_a_question_answered_after_the_build_leaves_the_packet(self) -> None:
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        plan.answer(QUESTION)
        loaded = plan.load(built.incident_id)

        self.assertEqual((QUESTION,), built.questions)
        self.assertEqual((), loaded.questions)

    def test_a_source_that_moved_is_reported_as_changed(self) -> None:
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        plan.write_state(pause_reason="quota_exhausted")
        loaded = plan.load(built.incident_id)

        status = loaded.source("status")
        assert status is not None
        self.assertTrue(status.changed)
        self.assertIn("quota_exhausted", status.summary)
        # Both families are projections of the one state record, so a write to
        # it moves both. Naming only `status` here would assert that a digest is
        # per-family when it is per-file.
        self.assertEqual({"status", "run_config"}, {item.kind for item in loaded.stale})

    def test_a_source_deleted_after_the_build_becomes_a_gap(self) -> None:
        """A recorded reference that no longer resolves is reported, not dropped."""
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        locator = built.source("verification").locator  # type: ignore[union-attr]
        (plan.plan_state / locator).unlink()
        loaded = plan.load(built.incident_id)

        self.assertEqual("unreadable", _gaps(loaded)["verification"])
        self.assertNotIn("verification", _kinds(loaded))
        self.assertFalse(loaded.complete)

    def test_a_plan_discovery_can_no_longer_read_is_refused_outright(self) -> None:
        """Not a gap: without a record there is no scope to check the packet against."""
        plan = _Plan(self)
        plan.populate()
        built = plan.build()

        (plan.plan_state / events.STATE_FILE_NAME).unlink()
        loaded = plan.load(built.incident_id)

        self.assertEqual("plan_unobserved", loaded.refusal)
        self.assertEqual((), loaded.sources)


class BoundsTests(unittest.TestCase):
    """A packet is bounded by construction, and says when it left something out."""

    def test_fifty_open_questions_stay_inside_the_summary_cap(self) -> None:
        plan = _Plan(self)
        for index in range(50):
            plan.ask(f"question {index} {MARKER}", question_id=question_uuid(index))

        source = plan.build().source("questions")

        assert source is not None
        self.assertLessEqual(len(source.summary), overseer_context.MAX_SUMMARY_CHARS)
        self.assertTrue(source.truncated)
        self.assertIn("50 open questions", source.summary)

    def test_the_identities_follow_the_same_cap(self) -> None:
        plan = _Plan(self)
        for index in range(50):
            plan.ask(f"question {index}", question_id=question_uuid(index))

        packet = plan.build()

        self.assertEqual(overseer_context.MAX_RECORDS, len(packet.questions))

    def test_a_huge_guidance_never_reaches_the_summary(self) -> None:
        plan = _Plan(self)
        plan.guide(MARKER * 5000)

        source = plan.build().source("supervision")

        assert source is not None
        self.assertLessEqual(len(source.summary), overseer_context.MAX_SUMMARY_CHARS)
        self.assertNotIn(MARKER, source.summary)

    def test_expansion_is_capped_and_says_it_was_capped(self) -> None:
        plan = _Plan(self)
        plan.guide(MARKER * 5000)
        packet = plan.build()

        excerpt = plan.expand(packet.incident_id, "supervision", limit=200)

        self.assertLessEqual(len(excerpt.text), 200)
        self.assertTrue(excerpt.truncated)
        self.assertIsNone(excerpt.refusal)

    def test_expansion_shows_more_than_the_summary_did(self) -> None:
        plan = _Plan(self)
        for index in range(10):
            plan.ask(f"question {index}", question_id=question_uuid(index))
        packet = plan.build()

        excerpt = plan.expand(packet.incident_id, "questions")

        self.assertIn(question_uuid(9), excerpt.text)
        self.assertNotIn(question_uuid(9), packet.source("questions").summary)  # type: ignore[union-attr]
        self.assertFalse(excerpt.truncated)

    def test_expanding_reports_a_source_that_moved(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.write_state(pause_reason="quota_exhausted")
        excerpt = plan.expand(packet.incident_id, "status")

        self.assertTrue(excerpt.changed)
        self.assertIn("quota_exhausted", excerpt.text)

    def test_expanding_a_family_the_incident_does_not_carry_is_refused(self) -> None:
        plan = _Plan(self)

        packet = plan.build()
        excerpt = plan.expand(packet.incident_id, "verification")

        self.assertEqual("evidence_missing", excerpt.refusal)
        self.assertEqual("", excerpt.text)

    def test_a_locator_that_escapes_the_state_directory_is_not_followed(self) -> None:
        """A stored row is data. Data that names ``../`` is not a reference."""
        plan = _Plan(self)
        outside = plan.root / "outside.json"
        outside.write_text(json.dumps({"marker": MARKER}), encoding="utf-8")
        plan.ledger.note_policy(config_fingerprint=FINGERPRINT)
        incident = plan.ledger.open_incident(
            grant=plan.grant(),
            plan_dir=str(plan.plan_dir),
            run_id=plan.run_id,
            kind="blocked",
            summary="hand-written evidence",
            evidence=(
                overseer_store_models.EvidenceInput(
                    alias=overseer_context.STATE_SPACE,
                    kind="status",
                    locator="../../../outside.json",
                    digest="sha256:whatever",
                ),
            ),
        )

        excerpt = plan.expand(incident.incident_id, "status")

        self.assertEqual("source_unreadable", excerpt.refusal)
        self.assertNotIn(MARKER, excerpt.text)


class ScopeTests(unittest.TestCase):
    """A packet is authorized against a repository set and a run."""

    def test_an_unchanged_plan_revalidates(self) -> None:
        plan = _Plan(self)
        plan.populate()

        checked = overseer_context.revalidate(
            packet=plan.build(), candidate=plan.candidate()
        )

        self.assertTrue(checked.fresh)
        self.assertIsNone(checked.reason)

    def test_a_moved_repository_root_changes_the_scope(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(
            repositories=[
                {
                    "alias": "scripts",
                    "path": "scripts",
                    "origin": "github.com/sglavoie/scripts",
                    "resolved_root": str(plan.root / "elsewhere"),
                }
            ]
        )
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("scope_changed", checked.reason)
        self.assertNotEqual(checked.recorded_scope, checked.current_scope)

    def test_a_new_registry_digest_changes_the_scope(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(registry_digest="e" * 64)
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("scope_changed", checked.reason)

    def test_a_replaced_run_is_not_the_run_the_packet_describes(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(run_id="run-2")
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertEqual("run_replaced", checked.reason)

    def test_a_pause_is_a_change_of_state_and_not_of_scope(self) -> None:
        """Every packet would be stale on arrival if lifecycle counted."""
        plan = _Plan(self)
        packet = plan.build()

        plan.write_state(paused=False, pause_reason=None)
        checked = overseer_context.revalidate(packet=packet, candidate=plan.candidate())

        self.assertTrue(checked.fresh)

    def test_an_unobservable_plan_is_unproven_rather_than_unchanged(self) -> None:
        plan = _Plan(self)
        packet = plan.build()

        (plan.plan_state / events.STATE_FILE_NAME).unlink()
        checked = overseer_context.revalidate(
            packet=packet, candidate=plan._candidate()
        )

        self.assertEqual("plan_unobserved", checked.reason)

    def test_loading_refuses_a_packet_whose_scope_moved(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        plan.write_state(registry_digest="e" * 64)
        loaded = plan.load(packet.incident_id)

        self.assertEqual("scope_changed", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_loading_refuses_a_packet_whose_run_was_replaced(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.write_state(run_id="run-2")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("run_replaced", loaded.refusal)


class AuthorizationTests(unittest.TestCase):
    """Every operation asks first, and a refusal reads nothing."""

    def test_building_without_a_grant_reads_nothing_and_opens_nothing(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(grant=None)

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual((), packet.sources)
        self.assertEqual(0, plan.mailbox_reads)
        self.assertEqual((), plan.ledger.incidents(plan_dir=str(plan.plan_dir)))

    def test_a_grant_for_another_plan_is_not_a_grant_for_this_one(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(grant=plan.grant(plan_dir=str(plan.root / "elsewhere")))

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_mutation_grant_does_not_authorize_a_context_read(self) -> None:
        plan = _Plan(self)
        plan.populate()

        packet = plan.build(
            grant=plan.grant(access=overseer_policy_client.ACCESS_MUTATION)
        )

        self.assertEqual("unauthorized", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_loading_without_a_grant_is_refused_before_any_read(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        loaded = plan.load(packet.incident_id, grant=None)

        self.assertEqual("unauthorized", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_expanding_without_a_grant_is_refused_before_any_read(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        excerpt = plan.expand(packet.incident_id, "questions", grant=None)

        self.assertEqual("unauthorized", excerpt.refusal)
        self.assertEqual("", excerpt.text)
        self.assertEqual(0, plan.mailbox_reads)

    def test_an_ineligible_run_is_never_assembled(self) -> None:
        """Identity comes before authorization, so there is nothing to ask about."""
        plan = _Plan(self)
        plan.write_state(registry_digest=None)

        with self.assertRaises(overseer_context.ContextError):
            plan.build()


class ExclusionTests(unittest.TestCase):
    """What an operator excludes cannot be loaded or expanded afterwards."""

    def test_a_revoked_plan_cannot_have_its_packet_loaded(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.mailbox_reads = 0

        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("enrollment_revoked", loaded.refusal)
        self.assertEqual((), loaded.sources)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_revoked_plan_cannot_have_its_packet_expanded(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        excerpt = plan.expand(packet.incident_id, "status")

        self.assertEqual("enrollment_revoked", excerpt.refusal)
        self.assertEqual("", excerpt.text)

    def test_a_revoked_plan_cannot_have_a_new_packet_built(self) -> None:
        plan = _Plan(self)
        plan.populate()
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")

        packet = plan.build()

        self.assertEqual("enrollment_revoked", packet.refusal)
        self.assertEqual(0, plan.mailbox_reads)

    def test_a_policy_change_seals_a_packet_against_its_old_grant(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.note_policy(config_fingerprint="fp-2")
        loaded = plan.load(packet.incident_id)

        self.assertEqual("policy_superseded", loaded.refusal)

    def test_a_policy_change_seals_a_packet_against_a_new_grant_too(self) -> None:
        """The new grant is valid. The evidence was gathered under the old one."""
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()

        plan.ledger.note_policy(config_fingerprint="fp-2")
        loaded = plan.load(packet.incident_id, grant=plan.grant(fingerprint="fp-2"))

        self.assertEqual("evidence_missing", loaded.refusal)

    def test_an_explicit_release_is_what_makes_sealed_evidence_readable(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.note_policy(config_fingerprint="fp-2")
        fresh = plan.grant(fingerprint="fp-2")

        released = plan.ledger.release_evidence(
            plan_dir=str(plan.plan_dir), grant=fresh
        )
        loaded = plan.load(packet.incident_id, grant=fresh)

        self.assertIsNone(released.refusal)
        self.assertIsNone(loaded.refusal)
        self.assertEqual(set(overseer_context.SOURCE_KINDS), _kinds(loaded))


class PermanentExclusionTests(unittest.TestCase):
    """Exclusion outlives the policy that was in force when it happened."""

    def test_a_revoked_plan_can_never_release_its_sealed_evidence(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.note_policy(config_fingerprint="fp-2")
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")

        released = plan.ledger.release_evidence(
            plan_dir=str(plan.plan_dir), grant=plan.grant(fingerprint="fp-2")
        )
        loaded = plan.load(packet.incident_id, grant=plan.grant(fingerprint="fp-2"))

        self.assertEqual("enrollment_revoked", released.refusal)
        self.assertEqual(0, released.released)
        self.assertEqual("enrollment_revoked", loaded.refusal)

    def test_exclusion_survives_reopening_the_ledger(self) -> None:
        plan = _Plan(self)
        plan.populate()
        packet = plan.build()
        plan.ledger.revoke(plan_dir=str(plan.plan_dir), reason="operator excluded it")
        plan.ledger.close()

        reopened = overseer_store_ledger.open_ledger(
            state_dir=plan.state_root, now=plan.iso_now
        )
        self.addCleanup(reopened.close)
        loaded = plan.load(packet.incident_id, ledger=reopened)

        self.assertEqual("enrollment_revoked", loaded.refusal)


class ContractTests(unittest.TestCase):
    """The published vocabularies are the ones the type says they are."""

    def test_every_source_kind_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.SourceKind)),
            set(overseer_context.SOURCE_KINDS),
        )

    def test_every_gap_reason_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.GapReason)),
            set(overseer_context.GAP_REASONS),
        )

    def test_every_staleness_reason_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.Staleness)),
            set(overseer_context.STALENESS_REASONS),
        )

    def test_every_refusal_is_published(self) -> None:
        self.assertEqual(
            set(get_args(overseer_context.Refusal)),
            set(overseer_context.REFUSALS),
        )

    def test_the_ledger_withholdings_are_reported_unparaphrased(self) -> None:
        """A refusal an operator reads must be the ledger's word for it."""
        self.assertLessEqual(
            set(overseer_store_models.WITHHOLDING_REASONS),
            set(overseer_context.REFUSALS),
        )

    def test_every_staleness_reason_is_also_a_refusal(self) -> None:
        self.assertLessEqual(
            set(overseer_context.STALENESS_REASONS),
            set(overseer_context.REFUSALS),
        )

    def test_every_family_says_what_its_absence_means(self) -> None:
        self.assertEqual(
            set(overseer_context.SOURCE_KINDS),
            set(overseer_context._ABSENT_DETAIL),
        )

    def test_context_is_asked_for_under_the_context_category(self) -> None:
        self.assertEqual(
            overseer_policy_client.ACCESS_CONTEXT, overseer_context.CONTEXT_ACCESS
        )

    def test_the_boundary_never_reaches_back_into_the_runner(self) -> None:
        assert_no_runner_import(self, overseer_context)

    def test_assembling_context_spawns_no_process(self) -> None:
        plan = _Plan(self)
        plan.populate()

        with mock.patch.object(
            subprocess, "run", side_effect=AssertionError("spawned a process")
        ):
            packet = plan.build()
            plan.load(packet.incident_id)
            plan.expand(packet.incident_id, "status")

        self.assertIsNone(packet.refusal)

    def test_assembling_context_writes_nothing_into_the_plan(self) -> None:
        """The only durable record is the ledger's. The corpus is read-only."""
        plan = _Plan(self)
        plan.populate()
        before = {
            path: path.read_bytes()
            for path in sorted(plan.plan_state.rglob("*"))
            if path.is_file()
        }

        packet = plan.build()
        plan.load(packet.incident_id)
        plan.expand(packet.incident_id, "questions")

        after = {
            path: path.read_bytes()
            for path in sorted(plan.plan_state.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
