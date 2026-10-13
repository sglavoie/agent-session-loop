"""Metadata-only fleet discovery: what it sees, what it refuses to open.

Two claims carry this boundary, and neither is provable by reading the module.

The first is *reach*: the overseer watches a fleet, so the inventory has to
enumerate every run in the corpus. The bridge caps its projection at
``bridge.MAX_ROWS`` because it serializes a queue view onto a wire, and a run
dropped past that cap is indistinguishable from a run that does not exist.
``BeyondTheBridgeLimitTests`` builds a corpus larger than the cap and asserts the
whole of it comes back.

The second is *restraint*: everything before AgentMux answers has to be minimal
identity metadata, so project files, harness transcripts, turn records and
review artifacts must stay closed. ``MetadataOnlyAccessTests`` stages all four
next to a real run and records every path the inventory opens, which is a claim
about behaviour rather than about the import list.

The rest pins the vocabulary an operator and the authorization caller read:
which records are eligible, which are legacy, and -- the rule that decides the
awkward cases -- that an observation this boundary could not complete is
missing evidence rather than proof that a run ended.

Nothing here spawns a process or touches a repository, which is what keeps this
module in `PORTABLE_MODULES`; `test_building_an_inventory_spawns_no_process`
asserts that rather than assuming it.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import socket
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    bridge,
    events,
    overseer_discovery,
    plans_files,
    plans_snapshot,
    run_payloads,
    run_selection,
    run_termination,
    state,
)

from .support import _WORKBENCH_DEAD_PID, write_plan

#: A timestamp far enough in the past that no test's window includes it by
#: accident, and late enough to be an ordinary run rather than an obvious
#: sentinel.
PAUSED_AT = "2026-06-19T14:00:00+00:00"
STARTED_AT = "2026-06-19T13:00:00+00:00"
FINISHED_AT = "2026-06-19T14:30:00+00:00"


def _repositories(root: Path) -> list[dict[str, str]]:
    """The repository projection `run_prompts._resolved_repositories_payload` writes."""
    return [
        {
            "alias": "scripts",
            "path": "scripts",
            "origin": "github.com/sglavoie/scripts",
            "resolved_root": str(root / "scripts"),
        }
    ]


class _Corpus:
    """An ASL state root plus the plan directories its records point at.

    Plan directories are created but never populated with anything discovery is
    allowed to read, except where a test deliberately stages a file it must not
    open. A plan is addressed by the same `state.plan_state_dir` the runner
    uses, so a record lands where a real run would have written it.
    """

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory()
        case.addCleanup(tmp.cleanup)
        # `plans_files.canonicalize_path` resolves the macOS `/var` symlink, and the
        # state directory name is a hash of the canonical path, so the fixture
        # has to agree with it before anything is written.
        self.root = plans_files.canonicalize_path(tmp.name)
        self.plans_root = self.root / "plans"
        self.plans_root.mkdir()
        self.state_root = self.root / "state"

    def plan_dir(self, name: str) -> Path:
        plan_dir = self.plans_root / name
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plans_files.canonicalize_path(plan_dir)

    def published_record(self, **overrides: Any) -> dict[str, Any]:
        """A record shaped the way a run born after this session publishes one."""
        record: dict[str, Any] = {
            "run_id": "20260619T130000Z-1",
            "harness": "claude",
            "ok": True,
            "paused": False,
            "dry_run": False,
            "run_started_at": STARTED_AT,
            "run_finished_at": None,
            "workspace_root": str(self.root),
            "registry_digest": "d" * 64,
            "repositories": _repositories(self.root),
            overseer_discovery.PAUSED_AT_KEY: None,
            overseer_discovery.DISCOVERY_VERSION_KEY: (
                overseer_discovery.DISCOVERY_CONTRACT_VERSION
            ),
        }
        record.update(overrides)
        return record

    def write_run(self, name: str, **overrides: Any) -> Path:
        plan_dir = self.plan_dir(name)
        record = self.published_record(**overrides)
        record.setdefault("plan_dir", str(plan_dir))
        self.write_raw_record(plan_dir, json.dumps(record, sort_keys=True))
        return plan_dir

    def write_raw_record(self, plan_dir: Path, text: str) -> Path:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        state_path = plan_state / events.STATE_FILE_NAME
        state_path.write_text(text, encoding="utf-8")
        return state_path

    def write_lock(self, plan_dir: Path, **fields: Any) -> Path:
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = plan_state / state.LOCK_FILE_NAME
        payload: dict[str, Any] = {
            "version": 3,
            "owner": "agent-session-loop",
            "plan_dir": str(plan_dir),
            "created_at": STARTED_AT,
            **fields,
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def inventory(self) -> overseer_discovery.Inventory:
        return overseer_discovery.build_inventory(state_dir=self.state_root)

    def only(self) -> overseer_discovery.Candidate:
        inventory = self.inventory()
        if len(inventory.candidates) != 1:
            raise AssertionError(
                f"expected one candidate, found {len(inventory.candidates)}"
            )
        return inventory.candidates[0]


class BeyondTheBridgeLimitTests(unittest.TestCase):
    """The inventory is a fleet view, not a queue frame."""

    def test_every_run_past_the_bridge_row_cap_is_enumerated(self) -> None:
        corpus = _Corpus(self)
        expected = bridge.MAX_ROWS + 25
        for index in range(expected):
            corpus.write_run(f"plan-{index:04d}", run_id=f"20260619T130000Z-{index}")

        inventory = corpus.inventory()

        self.assertGreater(expected, bridge.MAX_ROWS)
        self.assertEqual(expected, len(inventory.candidates))
        self.assertEqual(expected, len(inventory.eligible()))
        self.assertTrue(inventory.complete)
        self.assertEqual((), inventory.warnings)

    def test_the_inventory_carries_no_truncation_ceiling_of_its_own(self) -> None:
        """A cap anywhere in this module would be the bug the reach test hides.

        The test above would still pass against a limit set above its corpus
        size, so the ceiling itself is asserted absent: the module names no
        bound on how many candidates it returns.
        """
        source = Path(overseer_discovery.__file__ or "").read_text(encoding="utf-8")
        tree = ast.parse(source)
        slices = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice)
        ]

        self.assertEqual([], slices, "the inventory truncates its candidate list")

    def test_the_inventory_is_not_built_on_the_bridge_or_the_workbench(self) -> None:
        """Full summaries are the read this boundary exists to avoid.

        `workbench.build_workbench_summary` opens turn records, last-message
        files and review artifacts, and `bridge` is the module that bounds them
        for display. Importing either would put both back within reach of a
        boundary that runs before authorization.
        """
        source = Path(overseer_discovery.__file__ or "").read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                if node.module:
                    imported.add(node.module.split(".")[-1])

        self.assertEqual(set(), imported & {"bridge", "workbench", "runner"})


class MetadataOnlyAccessTests(unittest.TestCase):
    """Only ``state.json`` and ``lock.json`` are ever opened."""

    def _paths_opened_under(self, corpus: _Corpus) -> list[str]:
        """Every path inside the fixture that the inventory opened.

        Paths outside the corpus are dropped rather than asserted on: the
        interpreter opens its own files during a call and none of them are this
        boundary's business. What is asserted is total *within* the fixture,
        which is where every file discovery must not read was staged.
        """
        opened: list[str] = []
        real_open = builtins.open

        def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            opened.append(str(file))
            return real_open(file, *args, **kwargs)

        # `pathlib` reaches `io.open` through the `io` module rather than
        # through the builtins namespace, so both names are patched: they are
        # the same function, and patching only one would leave the other a hole.
        with (
            mock.patch.object(builtins, "open", recording_open),
            mock.patch.object(io, "open", recording_open),
        ):
            corpus.inventory()
        return [path for path in opened if path.startswith(str(corpus.root))]

    def test_project_contents_transcripts_and_review_evidence_stay_closed(
        self,
    ) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        # A real plan directory, complete with the artifacts a full workbench
        # summary would read.
        write_plan(plan_dir, ("pending", "pending"))
        (plan_dir / "final-review.md").write_text("# Review\n", encoding="utf-8")
        plan_state = state.plan_state_dir(plan_dir, corpus.state_root)
        run_dir = plan_state / "runs" / "20260619T130000Z-1" / "turn-1"
        run_dir.mkdir(parents=True)
        (run_dir / "result.json").write_text("{}", encoding="utf-8")
        (run_dir / "last-message.txt").write_text("secret", encoding="utf-8")
        (plan_state / events.EVENTS_FILE_NAME).write_text("{}\n", encoding="utf-8")
        corpus.write_lock(plan_dir, pid=_WORKBENCH_DEAD_PID)

        touched = sorted(set(self._paths_opened_under(corpus)))

        permitted = {
            str(plan_state / events.STATE_FILE_NAME),
            str(plan_state / state.LOCK_FILE_NAME),
        }
        self.assertEqual(sorted(permitted), touched)

    def test_observation_leaves_a_dead_local_lock_where_it_found_it(self) -> None:
        """Reclaim is hygiene, and an inventory is not entitled to perform it."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        lock_path = corpus.write_lock(
            plan_dir, pid=_WORKBENCH_DEAD_PID, hostname=socket.gethostname()
        )

        candidate = corpus.only()

        self.assertTrue(lock_path.exists(), "discovery removed a lock")
        self.assertFalse(candidate.ownership.alive)
        self.assertTrue(candidate.ownership.lock_present)


class EligibilityTests(unittest.TestCase):
    """Eligible means askable, and every refusal is nameable."""

    def test_a_published_record_is_eligible(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")

        candidate = corpus.only()

        self.assertTrue(candidate.eligible)
        self.assertEqual((), candidate.ineligible_reasons)
        self.assertEqual(str(plan_dir), candidate.plan_dir)
        self.assertEqual("20260619T130000Z-1", candidate.run_id)
        self.assertEqual(str(corpus.root), candidate.workspace_root)
        self.assertEqual("d" * 64, candidate.registry_digest)
        self.assertEqual(
            (
                overseer_discovery.RepositoryDeclaration(
                    alias="scripts",
                    path="scripts",
                    origin="github.com/sglavoie/scripts",
                    resolved_root=str(corpus.root / "scripts"),
                ),
            ),
            candidate.repositories,
        )
        self.assertEqual(STARTED_AT, candidate.run_started_at)

    def test_a_legacy_record_that_predates_the_contract_is_ineligible(self) -> None:
        """No published version means old, and old identity is not trustworthy."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-legacy")
        corpus.write_raw_record(
            plan_dir,
            json.dumps(
                {
                    "plan_dir": str(plan_dir),
                    "run_id": "20260101T000000Z-1",
                    "paused": False,
                    "run_started_at": STARTED_AT,
                }
            ),
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertIn("legacy_record", candidate.ineligible_reasons)
        self.assertIn("repositories_undeclared", candidate.ineligible_reasons)
        self.assertIn("registry_digest_missing", candidate.ineligible_reasons)
        self.assertTrue(candidate.record_observed)

    def test_a_record_from_a_newer_contract_is_refused_rather_than_guessed_at(
        self,
    ) -> None:
        corpus = _Corpus(self)
        corpus.write_run(
            "plan-future",
            **{
                overseer_discovery.DISCOVERY_VERSION_KEY: (
                    overseer_discovery.DISCOVERY_CONTRACT_VERSION + 1
                )
            },
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertEqual(("unsupported_contract",), candidate.ineligible_reasons)

    def test_a_record_naming_another_plan_is_ambiguous_identity(self) -> None:
        """The state directory name is the hash of the plan it belongs to."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        other = corpus.plan_dir("plan-beta")
        corpus.write_raw_record(
            plan_dir,
            json.dumps(corpus.published_record(plan_dir=str(other))),
        )

        candidate = corpus.only()

        self.assertFalse(candidate.eligible)
        self.assertEqual(("plan_identity_mismatch",), candidate.ineligible_reasons)

    def test_a_record_with_no_run_identity_is_ineligible(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_id="")

        candidate = corpus.only()

        self.assertEqual(("run_identity_missing",), candidate.ineligible_reasons)

    def test_a_run_that_declared_no_repositories_is_ineligible(self) -> None:
        """Every repository in a plan must be permitted, so none is not a grant."""
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", workspace_root=None, repositories=None)

        candidate = corpus.only()

        self.assertEqual(("repositories_undeclared",), candidate.ineligible_reasons)
        self.assertEqual((), candidate.repositories)

    def test_a_declaration_missing_its_resolved_root_refuses_the_whole_run(
        self,
    ) -> None:
        """Authorizing the readable subset would be the bypass, not the fix."""
        corpus = _Corpus(self)
        declarations = _repositories(corpus.root)
        declarations.append({"alias": "agentmux", "path": "1_dev/agentmux"})
        corpus.write_run("plan-alpha", repositories=declarations)

        candidate = corpus.only()

        self.assertEqual(
            ("repository_declaration_malformed",), candidate.ineligible_reasons
        )
        self.assertEqual(("scripts",), tuple(r.alias for r in candidate.repositories))

    def test_a_dry_run_is_not_a_fleet_member(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", dry_run=True)

        candidate = corpus.only()

        self.assertEqual(("dry_run",), candidate.ineligible_reasons)

    def test_every_named_reason_is_in_the_published_vocabulary(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-ok")
        corpus.write_run("plan-dry", dry_run=True)
        corpus.write_run("plan-nolabel", run_id="")

        for candidate in corpus.inventory().candidates:
            for reason in candidate.ineligible_reasons:
                with self.subTest(reason=reason):
                    self.assertIn(reason, overseer_discovery.INELIGIBILITY_REASONS)


class IncompleteEvidenceTests(unittest.TestCase):
    """A gap in observation is never read as a run that ended."""

    def test_a_malformed_record_is_unknown_rather_than_finished(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        corpus.write_raw_record(plan_dir, "{not json")

        inventory = corpus.inventory()
        candidate = inventory.candidates[0]

        self.assertEqual("unknown", candidate.lifecycle)
        self.assertFalse(candidate.record_observed)
        self.assertFalse(inventory.complete)
        self.assertEqual(("record_unobserved",), candidate.ineligible_reasons)
        self.assertEqual(1, len(inventory.warnings))
        self.assertIn("is malformed", inventory.warnings[0])

    def test_a_state_directory_with_no_record_yet_is_unknown(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        state.plan_state_dir(plan_dir, corpus.state_root).mkdir(parents=True)

        inventory = corpus.inventory()

        self.assertEqual("unknown", inventory.candidates[0].lifecycle)
        self.assertFalse(inventory.complete)
        # An absent record is an ordinary state of a directory a run has only
        # just created, so it is a gap without being news.
        self.assertEqual((), inventory.warnings)

    def test_a_json_document_that_is_not_an_object_is_unobserved(self) -> None:
        corpus = _Corpus(self)
        corpus.write_raw_record(corpus.plan_dir("plan-alpha"), "[]")

        inventory = corpus.inventory()

        self.assertFalse(inventory.candidates[0].record_observed)
        self.assertIn("is not a JSON object", inventory.warnings[0])

    def test_a_run_with_no_finish_and_no_live_owner_is_unproven(self) -> None:
        """A killed runner leaves exactly this record, and it is not a finish."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha", run_finished_at=None)
        corpus.write_lock(
            plan_dir, pid=_WORKBENCH_DEAD_PID, hostname=socket.gethostname()
        )

        candidate = corpus.only()

        self.assertEqual("unknown", candidate.lifecycle)
        self.assertTrue(candidate.eligible)

    def test_an_unreadable_state_root_yields_an_incomplete_empty_inventory(
        self,
    ) -> None:
        corpus = _Corpus(self)
        corpus.state_root.write_text("not a directory", encoding="utf-8")

        inventory = corpus.inventory()

        self.assertEqual((), inventory.candidates)
        self.assertFalse(inventory.complete)
        self.assertIn("state corpus is unreadable", inventory.warnings[0])

    def test_a_missing_state_root_scans_as_an_empty_but_complete_fleet(self) -> None:
        corpus = _Corpus(self)

        inventory = corpus.inventory()

        self.assertEqual((), inventory.candidates)
        self.assertTrue(inventory.complete)


class LifecycleAndOwnershipTests(unittest.TestCase):
    """Timestamps, pause state, and who holds the lock."""

    def test_a_finished_run_reports_its_finish(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_finished_at=FINISHED_AT)

        candidate = corpus.only()

        self.assertEqual("finished", candidate.lifecycle)
        self.assertEqual(FINISHED_AT, candidate.run_finished_at)

    def test_a_live_local_owner_makes_an_unfinished_run_running(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(plan_dir, pid=1, hostname=socket.gethostname())

        candidate = corpus.only()

        self.assertEqual("running", candidate.lifecycle)
        self.assertTrue(candidate.ownership.alive)

    def test_a_pause_outranks_the_finish_its_runner_recorded(self) -> None:
        """A paused run does finish; the pause is the fact the window is about."""
        corpus = _Corpus(self)
        corpus.write_run(
            "plan-alpha",
            paused=True,
            ok=False,
            pause_reason="agent-question",
            run_finished_at=FINISHED_AT,
            **{overseer_discovery.PAUSED_AT_KEY: PAUSED_AT},
        )

        candidate = corpus.only()

        self.assertEqual("paused", candidate.lifecycle)
        self.assertTrue(candidate.paused)
        self.assertEqual("agent-question", candidate.pause_reason)
        self.assertEqual(PAUSED_AT, candidate.paused_at)

    def test_a_lock_from_another_host_leaves_liveness_unknown(self) -> None:
        """Nothing here can see another machine's process table."""
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(
            plan_dir,
            pid=1,
            hostname=f"not-{socket.gethostname()}",
            process_start="2026-06-19T13:00:00",
        )

        candidate = corpus.only()

        self.assertIsNone(candidate.ownership.alive)
        self.assertTrue(candidate.ownership.foreign_host)
        self.assertEqual("2026-06-19T13:00:00", candidate.ownership.process_start)
        self.assertEqual("unknown", candidate.lifecycle)

    def test_a_run_with_no_lock_is_unowned(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha")

        ownership = corpus.only().ownership

        self.assertFalse(ownership.lock_present)
        self.assertIsNone(ownership.pid)
        self.assertIsNone(ownership.alive)
        self.assertFalse(ownership.foreign_host)


class PauseWindowTests(unittest.TestCase):
    """The trailing window the overseer is invoked for."""

    def _paused(self, corpus: _Corpus, name: str, paused_at: Any) -> Any:
        corpus.write_run(
            name,
            paused=True,
            ok=False,
            pause_reason="agent-question",
            **{overseer_discovery.PAUSED_AT_KEY: paused_at},
        )
        return corpus.only()

    def test_a_recent_pause_is_inside_the_default_window(self) -> None:
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(
            corpus, "plan-alpha", (now - timedelta(hours=3)).isoformat()
        )

        self.assertTrue(candidate.paused_within(now=now))

    def test_a_pause_older_than_the_window_is_outside_it(self) -> None:
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(
            corpus, "plan-alpha", (now - timedelta(hours=30)).isoformat()
        )

        self.assertFalse(candidate.paused_within(now=now))
        self.assertTrue(
            candidate.paused_within(
                overseer_discovery.DEFAULT_PAUSE_WINDOW_SECONDS * 2, now=now
            )
        )

    def test_an_undated_pause_is_not_recent(self) -> None:
        """A pause this boundary cannot date excludes rather than includes."""
        corpus = _Corpus(self)
        now = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
        candidate = self._paused(corpus, "plan-alpha", None)

        self.assertEqual("paused", candidate.lifecycle)
        self.assertIsNone(candidate.paused_at)
        self.assertFalse(candidate.paused_within(now=now))

    def test_a_run_that_is_not_paused_is_never_in_the_pause_window(self) -> None:
        corpus = _Corpus(self)
        corpus.write_run("plan-alpha", run_finished_at=FINISHED_AT)

        self.assertFalse(corpus.only().paused_within())


class PublishedLifecycleMetadataTests(unittest.TestCase):
    """The run lifecycle publishes what discovery is not allowed to go find."""

    def test_the_pause_writer_stamps_when_the_pause_happened(self) -> None:
        payload: dict[str, Any] = {}

        run_termination.set_pause(
            payload, reason="agent-question", message="Which column?"
        )

        stamped = payload[overseer_discovery.PAUSED_AT_KEY]
        self.assertIsNotNone(stamped)
        parsed = datetime.fromisoformat(stamped)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertLess(
            abs((datetime.now(UTC) - parsed).total_seconds()),
            60,
            "the pause stamp is not the moment of the pause",
        )

    def test_a_later_pause_replaces_the_stamp_of_an_earlier_one(self) -> None:
        payload: dict[str, Any] = {overseer_discovery.PAUSED_AT_KEY: PAUSED_AT}

        run_termination.set_pause(payload, reason="verify-failed", message="checks")

        self.assertNotEqual(PAUSED_AT, payload[overseer_discovery.PAUSED_AT_KEY])

    def test_a_run_is_born_publishing_both_discovery_fields(self) -> None:
        seed = _initial_payload(self)

        self.assertIsNone(seed[overseer_discovery.PAUSED_AT_KEY])
        self.assertEqual(
            overseer_discovery.DISCOVERY_CONTRACT_VERSION,
            seed[overseer_discovery.DISCOVERY_VERSION_KEY],
        )

    def test_a_seeded_record_paused_by_the_runner_is_discoverable(self) -> None:
        """End to end: what the lifecycle writes is what the inventory reads."""
        corpus = _Corpus(self)
        plan_dir = corpus.plan_dir("plan-alpha")
        seed = _initial_payload(self)
        seed.update(
            {
                "plan_dir": str(plan_dir),
                "workspace_root": str(corpus.root),
                "registry_digest": "d" * 64,
                "repositories": _repositories(corpus.root),
            }
        )
        run_termination.set_pause(seed, reason="agent-question", message="Which?")
        corpus.write_raw_record(plan_dir, json.dumps(seed, sort_keys=True, default=str))

        candidate = corpus.only()

        self.assertTrue(candidate.eligible)
        self.assertEqual("paused", candidate.lifecycle)
        self.assertTrue(candidate.paused_within())


class PortabilityTests(unittest.TestCase):
    """Nothing at this boundary shells out, which is the PORTABLE_MODULES claim."""

    def test_building_an_inventory_spawns_no_process(self) -> None:
        corpus = _Corpus(self)
        plan_dir = corpus.write_run("plan-alpha")
        corpus.write_lock(plan_dir, pid=_WORKBENCH_DEAD_PID)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("this boundary must not spawn a process")

        with (
            mock.patch.object(subprocess, "Popen", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "call", refuse),
            mock.patch.object(subprocess, "check_call", refuse),
            mock.patch.object(subprocess, "check_output", refuse),
        ):
            self.assertEqual(1, len(corpus.inventory().candidates))


def _initial_payload(case: unittest.TestCase) -> dict[str, Any]:
    """The run-state document `run_payloads` seeds, built from a real plan.

    Assembled here rather than imported from `tests/test_asl_run_payloads.py`:
    the claim under test is what a *born* run publishes, so the seed has to come
    from the real builder with the real arguments a run start hands it.
    """
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    root = Path(tmp.name).resolve()
    plan_dir = root / "plan"
    write_plan(plan_dir, ("pending", "pending"))
    state_dir = root / "state"
    plan_state = state.plan_state_dir(plan_dir, state_dir)
    run_id = "20260619T130000Z-1"
    from agent_session_loop import cli

    args = cli.build_parser().parse_args(
        ["run", str(plan_dir), "--state-dir", str(state_dir)]
    )
    return run_payloads._initial_payload(
        args=args,
        snapshot=plans_snapshot.inspect_plan(plan_dir),
        state_dir=plan_state,
        run_dir=plan_state / "runs" / run_id,
        event_path=plan_state / events.EVENTS_FILE_NAME,
        state_path=plan_state / events.STATE_FILE_NAME,
        lock_info=state.LockInfo(
            path=plan_state / state.LOCK_FILE_NAME,
            pid=123,
            plan_dir=str(plans_files.canonicalize_path(plan_dir)),
            created_at=STARTED_AT,
            stale=False,
            reason="active",
            raw={},
        ),
        cwd=root,
        run_id=run_id,
        commit_mode=None,
        mailbox_activation=run_selection._mailbox_activation_payload(args, plan_dir),
    )


if __name__ == "__main__":
    unittest.main()
