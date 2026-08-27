"""Tests for scheduled storage maintenance in `agent_session_loop.maintenance`.

Maintenance is the only thing in ASL that deletes evidence on a timer, with no
human watching, so what these tests pin is mostly what it must *not* do:
never act under either threshold, never remove anything retention protects,
never remove anything on knowledge that was true before the lock was held,
never interleave with a second pass, never leave removed bytes without the
metadata describing them, and never report bytes as reclaimed that are still on
disk. The lanes it *is* allowed to use — expired run reclaim, and compression
then budgeted removal of a terminal newest run's reproducible detail — are
pinned as deterministic: the same corpus plans the same actions every time.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import plistlib
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import assurance_models as models
from agent_session_loop import assurance_projection as projection
from agent_session_loop import maintenance, plans, retention, state

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
AUDIT_STAMP = "2026-06-01T00:00:00+00:00"

RUN_ANCIENT = "20260101T000000000000Z-11"
RUN_OLD = "20260601T000000000000Z-22"
RUN_NEWEST = "20260803T000000000000Z-33"

# Thresholds the tests choose deliberately instead of inheriting, so a real
# machine's free space can never decide whether an assertion holds.
NEVER_LOW = 0.0
ALWAYS_LOW = 1.0
HUGE_BUDGET = 1 << 40


def _stamp(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class MaintenanceTestBase(unittest.TestCase):
    """Builds a throwaway state root holding one or more plans' run directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.state_root = self.tmp_path / "state"
        self.state_root.mkdir(parents=True)
        self.plan_dir = self._make_plan("plan")

    def _make_plan(self, name: str) -> Path:
        plan_dir = self.tmp_path / name
        plan_dir.mkdir()
        plan_state = state.plan_state_dir(plan_dir, self.state_root)
        plan_state.mkdir(parents=True)
        payload = {"plan_dir": str(plans.canonicalize_path(plan_dir))}
        (plan_state / "state.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        return plan_dir

    def _write_lock(self, *, plan_dir: Path | None = None, **fields: object) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        lock_path = state.plan_state_dir(target, self.state_root) / state.LOCK_FILE_NAME
        payload = {
            "plan_dir": str(plans.canonicalize_path(target)),
            "created_at": "2026-08-01T00:00:00+00:00",
            **fields,
        }
        lock_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return lock_path

    def _write_run(
        self,
        run_id: str,
        *,
        finished_at: str | None,
        plan_dir: Path | None = None,
        bodies: dict[str, bytes] | None = None,
        turns: int = 1,
        write_result: bool = True,
    ) -> Path:
        target = plan_dir if plan_dir is not None else self.plan_dir
        run_dir = (
            state.plan_state_dir(target, self.state_root) / state.RUNS_DIR_NAME / run_id
        )
        for index in range(1, turns + 1):
            turn_dir = run_dir / f"turn-{index:04d}-session-1"
            turn_dir.mkdir(parents=True)
            for name, data in (bodies or {"stderr.log": b""}).items():
                (turn_dir / name).write_bytes(data)
            if write_result:
                payload: dict[str, object] = {"status_result": "ok"}
                if finished_at is not None:
                    payload["finished_at"] = finished_at
                (turn_dir / "result.json").write_text(
                    json.dumps(payload, sort_keys=True), encoding="utf-8"
                )
        return run_dir

    def _run(self, **kwargs: Any) -> maintenance.MaintenanceResult:
        """Run one pass with thresholds pinned unless a test overrides them."""
        kwargs.setdefault("now", NOW)
        kwargs.setdefault("min_free_fraction", NEVER_LOW)
        kwargs.setdefault("budget_bytes", HUGE_BUDGET)
        kwargs.setdefault("force", True)
        return maintenance.run_maintenance(self.state_root, **kwargs)

    def _state_dir(self, plan_dir: Path | None = None) -> Path:
        return state.plan_state_dir(
            plan_dir if plan_dir is not None else self.plan_dir, self.state_root
        )

    def _run_path(self, run_id: str, plan_dir: Path | None = None) -> Path:
        return self._state_dir(plan_dir) / state.RUNS_DIR_NAME / run_id

    def _pin_run(self, run_id: str) -> None:
        """Open a real assurance issue against `run_id`, pinning its artifacts."""
        finding = models.Finding(
            category="lifecycle",
            component="agent_session_loop/runner.py",
            invariant_key="retry.budget",
            lane="lifecycle-retry-resume",
            severity="high",
            summary="A retry ran after the budget was exhausted.",
            recommendation="Clamp the retry counter before relaunching.",
            evidence=("events.jsonl",),
        )
        projection.publish_audit(
            projection.AuditPublication(
                run_id=run_id,
                state="findings",
                eligible=True,
                findings=(finding,),
                completed_at=AUDIT_STAMP,
            ),
            state_dir=self.state_root,
            recorded_at=AUDIT_STAMP,
        )


class ThresholdTests(MaintenanceTestBase):
    """Nothing happens until the corpus is over budget or the volume is low."""

    def test_under_both_thresholds_nothing_is_planned_or_touched(self) -> None:
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"x" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, force=False)

        self.assertEqual(result.outcome, maintenance.OUTCOME_UNDER_THRESHOLD)
        self.assertEqual(result.trigger, maintenance.TRIGGER_NONE)
        self.assertIsNone(result.plan)
        self.assertFalse(result.applied)
        self.assertTrue(result.ok)
        self.assertTrue(run_dir.is_dir())

    def test_corpus_over_budget_is_the_reported_trigger(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"x" * 8192}
        )

        usage = maintenance.measure_corpus(
            self.state_root, now=NOW, budget_bytes=4096, min_free_fraction=NEVER_LOW
        )

        self.assertTrue(usage.triggered)
        self.assertTrue(usage.over_budget)
        self.assertFalse(usage.low_free_space)
        self.assertEqual(usage.trigger, maintenance.TRIGGER_CORPUS_OVER_BUDGET)

    def test_low_free_space_triggers_even_when_the_corpus_is_small(self) -> None:
        usage = maintenance.measure_corpus(
            self.state_root,
            now=NOW,
            budget_bytes=HUGE_BUDGET,
            min_free_fraction=ALWAYS_LOW,
        )

        self.assertTrue(usage.triggered)
        self.assertFalse(usage.over_budget)
        self.assertTrue(usage.low_free_space)
        self.assertEqual(usage.trigger, maintenance.TRIGGER_LOW_FREE_SPACE)

    def test_shipped_thresholds_are_the_governing_decision(self) -> None:
        self.assertEqual(maintenance.CORPUS_BUDGET_BYTES, 8 * 1024**3)
        self.assertEqual(maintenance.MIN_FREE_FRACTION, 0.15)
        self.assertEqual(maintenance.COMPACTION_AGE_DAYS, 90.0)
        self.assertEqual(maintenance.LOG_BODY_BUDGET_BYTES, 64 * 1024**2)

    def test_a_missing_state_root_measures_zero_rather_than_failing(self) -> None:
        usage = maintenance.measure_corpus(
            self.tmp_path / "absent", now=NOW, min_free_fraction=NEVER_LOW
        )

        self.assertEqual(usage.corpus_bytes, 0)
        self.assertFalse(usage.over_budget)


class ProtectedEvidenceTests(MaintenanceTestBase):
    """Every retention fence still holds when maintenance is the one deleting."""

    def test_locked_live_pinned_and_recent_runs_all_survive_an_applied_pass(
        self,
    ) -> None:
        locked_plan = self._make_plan("locked")
        locked_run = self._write_run(
            RUN_OLD, finished_at=_stamp(200), plan_dir=locked_plan
        )
        self._write_lock(plan_dir=locked_plan, pid=os.getpid())

        recent = self._write_run(RUN_OLD, finished_at=_stamp(2))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        unfinished_plan = self._make_plan("unfinished")
        unfinished = self._write_run(
            RUN_ANCIENT, finished_at=None, plan_dir=unfinished_plan
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1), plan_dir=unfinished_plan)

        result = self._run(apply=True)

        self.assertEqual(result.outcome, maintenance.OUTCOME_APPLIED)
        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.reclaimed_bytes, 0)
        for survivor in (locked_run, recent, unfinished):
            self.assertTrue(survivor.is_dir(), f"{survivor} was removed")

    def test_an_open_assurance_issue_pins_its_run_against_maintenance(self) -> None:
        pinned = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"y" * 512}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._pin_run(RUN_OLD)

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(pinned.is_dir())

    def test_an_unreadable_assurance_ledger_stops_every_removal(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        # A missing ledger is a readable empty inbox; only one that exists and
        # cannot be parsed leaves the pin question unanswered.
        ledger.ledger_path(self.state_root).mkdir(parents=True)

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())

    def test_an_expired_unprotected_run_is_the_one_thing_removed(self) -> None:
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"z" * 2048}
        )
        newest = self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertGreaterEqual(result.reclaimed_bytes, 2048)
        self.assertFalse(expired.exists())
        self.assertTrue(newest.is_dir())
        self.assertTrue((self._state_dir() / "state.json").is_file())

    def test_dry_run_projects_the_same_removal_without_performing_it(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=False)

        self.assertEqual(result.outcome, maintenance.OUTCOME_PLANNED)
        self.assertFalse(result.applied)
        assert result.plan is not None
        self.assertEqual(result.plan.reclaim.reclaimable_run_count, 1)
        self.assertTrue(expired.is_dir())


class RevalidationTests(MaintenanceTestBase):
    """Liveness and pins are re-read under the lock, not trusted from before it.

    Re-read twice, in fact: once to narrow the plan, and once more immediately
    before each destructive action. Nothing that writes a plan lock, starts a
    run, or opens an assurance issue takes the maintenance claim, so a single
    revalidation only proves the corpus was safe to delete at one past instant.
    """

    def test_a_run_that_becomes_live_after_planning_is_dropped(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        plan_state = str(self._state_dir())

        calls: list[int] = []
        real = retention.live_run_keys

        def racing(state_dir: Any = None) -> frozenset[tuple[str, str]]:
            calls.append(1)
            if len(calls) == 1:
                return real(state_dir)
            # The revalidation read, and every read after it, sees the run come
            # alive between planning and mutation.
            return frozenset({(plan_state, RUN_OLD)})

        with unittest.mock.patch.object(retention, "live_run_keys", racing):
            result = self._run(apply=True)

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["run_id"], RUN_OLD)
        self.assertEqual(drop["lane"], "reclaim")
        self.assertIn("became live", drop["reason"])

    def test_a_plan_locked_after_planning_is_dropped(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        real = maintenance._revalidate

        def locking(*args: Any, **kwargs: Any) -> Any:
            self._write_lock(pid=os.getpid())
            return real(*args, **kwargs)

        with unittest.mock.patch.object(maintenance, "_revalidate", locking):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertIn("plan lock is active", drop["reason"])
        self.assertEqual(drop["stage"], maintenance.STAGE_REVALIDATION)

    def test_pin_acquired_after_initial_revalidation_blocks_removal(self) -> None:
        """A pin that lands between revalidation and removal still protects."""
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"p" * 512}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        real = maintenance._revalidate

        def revalidate_then_pin(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            # The issue is opened after the plan has been revalidated and
            # cleared for removal. Nothing but a reading taken at the moment of
            # deletion can see it, because the assurance writer never took the
            # maintenance claim and was free to land here.
            self._pin_run(RUN_OLD)
            return approved

        with unittest.mock.patch.object(
            maintenance, "_revalidate", revalidate_then_pin
        ):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.reclaimed_bytes, 0)
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["run_id"], RUN_OLD)
        self.assertEqual(drop["lane"], maintenance.LANE_RECLAIM)
        # Recorded as a mutation-time drop: the plan cleared this run and the
        # fence in front of the removal itself is what saved it.
        self.assertEqual(drop["stage"], maintenance.STAGE_MUTATION)
        self.assertIn("assurance pin", drop["reason"])

    def test_a_lock_acquired_after_initial_revalidation_blocks_removal(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        real = maintenance._revalidate

        def revalidate_then_lock(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            self._write_lock(pid=os.getpid())
            return approved

        with unittest.mock.patch.object(
            maintenance, "_revalidate", revalidate_then_lock
        ):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["stage"], maintenance.STAGE_MUTATION)
        self.assertIn("plan lock is active", drop["reason"])

    def test_a_pin_acquired_after_initial_revalidation_blocks_compaction(self) -> None:
        """The compaction lane is fenced too: compressing unlinks the original."""
        payload = b"repeat me " * 4096
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": payload}
        )
        real = maintenance._revalidate

        def revalidate_then_pin(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            self._pin_run(RUN_OLD)
            return approved

        with unittest.mock.patch.object(
            maintenance, "_revalidate", revalidate_then_pin
        ):
            result = self._run(apply=True)

        turn = run_dir / "turn-0001-session-1"
        self.assertTrue((turn / "stderr.log").is_file())
        self.assertEqual((turn / "stderr.log").read_bytes(), payload)
        self.assertFalse((turn / "stderr.log.gz").exists())
        self.assertEqual(result.compressed_body_count, 0)
        self.assertEqual(result.removed_body_count, 0)
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["lane"], maintenance.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance.STAGE_MUTATION)
        self.assertIn("assurance pin", drop["reason"])

    def test_pin_acquired_during_compression_blocks_source_unlink(self) -> None:
        """Compressing is slow, and a pin that lands while it runs still wins.

        The fence in front of the compression cleared this body; the pin opens
        after the gzip exists, which is precisely the window a fence taken only
        before the operation cannot see. The unlink is the destructive step, so
        that is where the reading has to be taken.
        """
        payload = b"protect me " * 4096
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": payload}
        )
        turn = run_dir / "turn-0001-session-1"
        real = maintenance._stage_compressed

        def stage_then_pin(source: Path, target: Path) -> tuple[Path, str, int]:
            staged = real(source, target)
            self._pin_run(RUN_OLD)
            return staged

        with unittest.mock.patch.object(
            maintenance, "_stage_compressed", stage_then_pin
        ):
            result = self._run(apply=True)

        # The source body is untouched, byte for byte.
        self.assertTrue((turn / "stderr.log").is_file())
        self.assertEqual((turn / "stderr.log").read_bytes(), payload)
        # Nothing was published either: no gzip, no sidecar, no stray temporary.
        self.assertFalse((turn / "stderr.log.gz").exists())
        self.assertFalse((turn / "stderr.log.body.json").exists())
        self.assertEqual(
            sorted(child.name for child in turn.iterdir()),
            ["result.json", "stderr.log"],
        )
        self.assertEqual(result.compressed_body_count, 0)
        self.assertEqual(result.removed_body_count, 0)
        self.assertEqual(result.failures, ())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["run_id"], RUN_OLD)
        self.assertEqual(drop["lane"], maintenance.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance.STAGE_MUTATION)
        self.assertIn("assurance pin", drop["reason"])

    def test_pin_acquired_after_the_gzip_is_published_keeps_the_original(self) -> None:
        """A pin landing after the sidecar is durable still saves the original.

        This is the second window: the gzip and its ledger are on disk, and only
        the unlink is left. What survives has to be true as well as intact — the
        sidecar describes a body that is present and says it was not removed.
        """
        payload = b"still here " * 4096
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": payload}
        )
        turn = run_dir / "turn-0001-session-1"
        real = maintenance._write_ledger

        def publish_then_pin(source: Path, ledger: dict[str, Any]) -> None:
            real(source, ledger)
            self._pin_run(RUN_OLD)

        with unittest.mock.patch.object(maintenance, "_write_ledger", publish_then_pin):
            result = self._run(apply=True)

        self.assertTrue((turn / "stderr.log").is_file())
        self.assertEqual((turn / "stderr.log").read_bytes(), payload)
        self.assertEqual(result.compressed_body_count, 0)
        self.assertEqual(result.failures, ())
        sidecar = json.loads(
            (turn / "stderr.log.body.json").read_text(encoding="utf-8")
        )
        self.assertFalse(sidecar["removed"])
        self.assertEqual(sidecar["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(sidecar["compressed_path"], str(turn / "stderr.log.gz"))
        # Truthful and recoverable: the sidecar's compressed copy really exists
        # and round-trips, so the next unprotected pass converges rather than
        # discovering a claim it cannot honour.
        self.assertEqual(
            gzip.decompress((turn / "stderr.log.gz").read_bytes()), payload
        )
        self.assertEqual(len(result.revalidation_drops), 1)
        self.assertEqual(
            result.revalidation_drops[0]["stage"], maintenance.STAGE_MUTATION
        )
        self.assertIn("assurance pin", result.revalidation_drops[0]["reason"])

    def test_pin_acquired_during_body_budgeting_blocks_body_unlink(self) -> None:
        """Digesting an over-budget body is preparation, not permission.

        The pin opens after the body has been digested and its tombstone made
        durable, so only a reading taken at the unlink itself can stop the
        removal — and the tombstone it already published has to be withdrawn,
        because a ledger claiming a removal that did not happen is evidence a
        human would act on.
        """
        payload = os.urandom(40_000)
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stdout.log": payload}
        )
        turn = run_dir / "turn-0001-session-1"
        real = maintenance._write_ledger
        pinned: list[int] = []

        def publish_then_pin(source: Path, ledger: dict[str, Any]) -> None:
            real(source, ledger)
            # The compression ledger is written first and must be allowed
            # through; the pin lands against the removal tombstone.
            if ledger.get("removed") and not pinned:
                pinned.append(1)
                self._pin_run(RUN_OLD)

        with unittest.mock.patch.object(maintenance, "_write_ledger", publish_then_pin):
            result = self._run(apply=True, log_body_budget_bytes=0)

        self.assertEqual(pinned, [1])
        # The compression completed before the pin existed, so the evidence now
        # lives in the gzip — and that is what must survive the removal lane.
        self.assertTrue((turn / "stdout.log.gz").is_file())
        self.assertEqual(
            gzip.decompress((turn / "stdout.log.gz").read_bytes()), payload
        )
        self.assertEqual(result.removed_body_count, 0)
        self.assertEqual(result.compaction_reclaimed_bytes, 0)
        self.assertEqual(result.failures, ())
        # The tombstone was withdrawn, so the sidecar and the disk agree.
        sidecar = json.loads(
            (turn / "stdout.log.body.json").read_text(encoding="utf-8")
        )
        self.assertFalse(sidecar["removed"])
        self.assertIsNone(sidecar["removed_at"])
        self.assertEqual(sidecar["reason"], "")
        self.assertNotIn("removed_path", sidecar)
        self.assertNotIn("removed_bytes", sidecar)
        self.assertEqual(sidecar["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["run_id"], RUN_OLD)
        self.assertEqual(drop["lane"], maintenance.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance.STAGE_MUTATION)
        self.assertIn("assurance pin", drop["reason"])

    def test_pin_acquired_before_the_tombstone_leaves_no_metadata_behind(self) -> None:
        """Caught during the digest, nothing about the body changes at all.

        A body compressed by an older version that left no sidecar has to be
        digested before it can be removed, and digesting it reads every byte.
        A pin that lands in that window stops the removal before any metadata
        is published, so there is nothing to walk back.
        """
        payload = os.urandom(40_000)
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stdout.log": payload}
        )
        turn = run_dir / "turn-0001-session-1"
        # Already compressed, with no ledger beside it: the one shape that still
        # makes the removal lane digest a body itself.
        maintenance._compress_file(turn / "stdout.log", turn / "stdout.log.gz")
        (turn / "stdout.log").unlink()
        real = maintenance._digest_file

        def digest_then_pin(path: Path) -> tuple[str, int]:
            digested = real(path)
            self._pin_run(RUN_OLD)
            return digested

        with unittest.mock.patch.object(maintenance, "_digest_file", digest_then_pin):
            result = self._run(apply=True, log_body_budget_bytes=0)

        self.assertTrue((turn / "stdout.log.gz").is_file())
        self.assertEqual(
            gzip.decompress((turn / "stdout.log.gz").read_bytes()), payload
        )
        self.assertEqual(result.removed_body_count, 0)
        self.assertEqual(result.failures, ())
        self.assertFalse((turn / "stdout.log.body.json").exists())
        self.assertEqual(len(result.revalidation_drops), 1)
        self.assertEqual(
            result.revalidation_drops[0]["stage"], maintenance.STAGE_MUTATION
        )


class CompactionTests(MaintenanceTestBase):
    """Terminal newest runs keep their authority and lose reproducible bulk."""

    def _newest_only(self, *, age_days: float, bodies: dict[str, bytes]) -> Path:
        """A plan whose one and only run is terminal, newest, and `age_days` old."""
        return self._write_run(RUN_OLD, finished_at=_stamp(age_days), bodies=bodies)

    def test_an_old_terminal_newest_run_has_its_bodies_compressed(self) -> None:
        payload = b"repeat me " * 4096
        run_dir = self._newest_only(
            age_days=200,
            bodies={
                "stdout.log": payload,
                "stderr.log": payload,
                "last-message.txt": b"done",
            },
        )
        turn = run_dir / "turn-0001-session-1"

        result = self._run(apply=True)

        self.assertEqual(result.compressed_body_count, 3)
        self.assertEqual(result.removed_body_count, 0)
        for name in maintenance.LOG_BODY_NAMES:
            self.assertFalse((turn / name).exists(), f"{name} was not replaced")
            self.assertTrue((turn / f"{name}.gz").is_file())
        self.assertEqual(
            gzip.decompress((turn / "stderr.log.gz").read_bytes()), payload
        )
        # The authoritative record is untouched in the lane that touches detail.
        self.assertEqual(
            json.loads((turn / "result.json").read_text(encoding="utf-8"))[
                "status_result"
            ],
            "ok",
        )

    def test_compaction_records_the_original_identity_of_every_body(self) -> None:
        payload = b"evidence " * 1000
        run_dir = self._newest_only(age_days=200, bodies={"stderr.log": payload})
        turn = run_dir / "turn-0001-session-1"

        self._run(apply=True)

        ledger = json.loads((turn / "stderr.log.body.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["path"], str(turn / "stderr.log"))
        self.assertEqual(ledger["byte_count"], len(payload))
        self.assertEqual(
            ledger["sha256"],
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(ledger["compressed_path"], str(turn / "stderr.log.gz"))
        self.assertFalse(ledger["removed"])

    def test_a_recent_terminal_newest_run_is_left_entirely_alone(self) -> None:
        run_dir = self._newest_only(
            age_days=89, bodies={"stderr.log": b"still recent" * 100}
        )
        turn = run_dir / "turn-0001-session-1"

        result = self._run(apply=True)

        assert result.plan is not None
        self.assertEqual(result.plan.compactions, ())
        self.assertEqual(result.compressed_body_count, 0)
        self.assertTrue((turn / "stderr.log").is_file())

    def test_a_protected_newest_run_is_never_compacted(self) -> None:
        run_dir = self._newest_only(age_days=200, bodies={"stderr.log": b"kept" * 500})
        self._write_lock(pid=os.getpid())
        turn = run_dir / "turn-0001-session-1"

        result = self._run(apply=True)

        self.assertEqual(result.compressed_body_count, 0)
        self.assertTrue((turn / "stderr.log").is_file())

    def test_bodies_above_the_budget_are_removed_largest_first(self) -> None:
        # Incompressible payloads, so post-compression sizes stay predictable
        # and the budget arithmetic is about the selection, not about gzip.
        big = os.urandom(40_000)
        medium = os.urandom(20_000)
        small = os.urandom(1_000)
        run_dir = self._newest_only(
            age_days=200,
            bodies={
                "stdout.log": big,
                "stderr.log": medium,
                "last-message.txt": small,
            },
        )
        turn = run_dir / "turn-0001-session-1"

        result = self._run(apply=True, log_body_budget_bytes=25_000)

        self.assertEqual(result.removed_body_count, 1)
        # The largest body goes first, so one removal is enough to fit.
        self.assertFalse((turn / "stdout.log.gz").exists())
        self.assertTrue((turn / "stderr.log.gz").is_file())
        self.assertTrue((turn / "last-message.txt.gz").is_file())

    def test_a_removed_body_leaves_path_bytes_digest_and_reason_behind(self) -> None:
        payload = os.urandom(40_000)
        run_dir = self._newest_only(age_days=200, bodies={"stdout.log": payload})
        turn = run_dir / "turn-0001-session-1"

        self._run(apply=True, log_body_budget_bytes=0)

        ledger = json.loads((turn / "stdout.log.body.json").read_text(encoding="utf-8"))
        self.assertTrue(ledger["removed"])
        self.assertEqual(ledger["reason"], maintenance.REASON_LOG_BODY_BUDGET)
        self.assertEqual(ledger["path"], str(turn / "stdout.log"))
        self.assertEqual(ledger["byte_count"], len(payload))
        self.assertEqual(ledger["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertIsNotNone(ledger["removed_at"])
        self.assertFalse((turn / "stdout.log").exists())
        self.assertFalse((turn / "stdout.log.gz").exists())

    def test_no_body_is_ever_removed_without_its_metadata(self) -> None:
        run_dir = self._newest_only(
            age_days=200,
            bodies={
                "stdout.log": os.urandom(30_000),
                "stderr.log": os.urandom(30_000),
                "last-message.txt": os.urandom(30_000),
            },
        )
        turn = run_dir / "turn-0001-session-1"

        self._run(apply=True, log_body_budget_bytes=0)

        for name in maintenance.LOG_BODY_NAMES:
            self.assertFalse((turn / name).exists())
            self.assertFalse((turn / f"{name}.gz").exists())
            ledger = json.loads(
                (turn / f"{name}.body.json").read_text(encoding="utf-8")
            )
            self.assertTrue(ledger["removed"])
            self.assertEqual(ledger["reason"], maintenance.REASON_LOG_BODY_BUDGET)
            self.assertTrue(ledger["sha256"])

    def test_selection_is_deterministic_across_repeated_planning(self) -> None:
        self._newest_only(
            age_days=200,
            bodies={
                "stdout.log": os.urandom(30_000),
                "stderr.log": os.urandom(30_000),
                "last-message.txt": os.urandom(30_000),
            },
        )

        def project() -> list[tuple[str, str]]:
            plan = maintenance.plan_maintenance(
                self.state_root,
                now=NOW,
                force=True,
                log_body_budget_bytes=35_000,
                usage=maintenance.measure_corpus(
                    self.state_root, now=NOW, min_free_fraction=NEVER_LOW
                ),
            )
            return [
                (action.path, action.action)
                for entry in plan.compactions
                for action in entry.actions
            ]

        self.assertEqual(project(), project())

    def test_a_second_pass_over_compacted_detail_changes_nothing(self) -> None:
        run_dir = self._newest_only(age_days=200, bodies={"stderr.log": b"c" * 8000})
        turn = run_dir / "turn-0001-session-1"

        self._run(apply=True)
        first = (turn / "stderr.log.gz").read_bytes()
        second_result = self._run(apply=True)

        self.assertEqual(second_result.compressed_body_count, 0)
        self.assertEqual(second_result.removed_body_count, 0)
        self.assertEqual((turn / "stderr.log.gz").read_bytes(), first)

    def test_an_interrupted_compression_is_recovered_on_the_next_pass(self) -> None:
        # A pass that died between writing the gzip and unlinking the original
        # leaves both on disk. The next pass must converge, not double-count.
        payload = b"interrupted " * 700
        run_dir = self._newest_only(age_days=200, bodies={"stderr.log": payload})
        turn = run_dir / "turn-0001-session-1"
        maintenance._compress_file(turn / "stderr.log", turn / "stderr.log.gz")

        result = self._run(apply=True)

        self.assertEqual(result.compressed_body_count, 1)
        self.assertFalse((turn / "stderr.log").exists())
        self.assertEqual(
            gzip.decompress((turn / "stderr.log.gz").read_bytes()), payload
        )


class PartialFailureTests(MaintenanceTestBase):
    """A failed mutation is reported, and its bytes are not claimed as freed."""

    def test_a_failed_run_removal_is_reported_and_not_counted(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"q" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        with unittest.mock.patch.object(
            retention, "remove_run_directory", return_value="Permission denied"
        ):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.reclaimed_bytes, 0)
        self.assertEqual(len(result.failures), 1)
        failure = result.failures[0]
        self.assertEqual(failure.kind, maintenance.FAILURE_RUN_REMOVAL)
        self.assertEqual(failure.error, "Permission denied")
        self.assertGreaterEqual(failure.bytes_still_allocated, 4096)
        self.assertIn(
            maintenance.HEALTH_PARTIAL_FAILURE,
            [finding.code for finding in result.health],
        )

    def test_a_partial_removal_counts_only_the_bytes_that_actually_went(self) -> None:
        run_dir = self._write_run(
            RUN_OLD,
            finished_at=_stamp(200),
            bodies={"stdout.log": b"a" * 4096, "stderr.log": b"b" * 4096},
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        turn = run_dir / "turn-0001-session-1"

        def half_removed(path: Path) -> str:
            (turn / "stdout.log").unlink()
            return "Directory not empty"

        with unittest.mock.patch.object(
            retention, "remove_run_directory", half_removed
        ):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.reclaimed_bytes, 4096)
        self.assertGreaterEqual(result.failures[0].bytes_still_allocated, 4096)

    def test_a_failed_compression_leaves_the_original_body_in_place(self) -> None:
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"w" * 6000}
        )
        turn = run_dir / "turn-0001-session-1"

        def refuse(source: Path, target: Path) -> tuple[Path, str, int]:
            raise OSError("No space left on device")

        with unittest.mock.patch.object(maintenance, "_stage_compressed", refuse):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertTrue((turn / "stderr.log").is_file())
        self.assertEqual(result.compressed_body_count, 0)
        self.assertEqual(result.failures[0].kind, maintenance.FAILURE_BODY_COMPRESS)
        self.assertGreaterEqual(result.failures[0].bytes_still_allocated, 6000)

    def test_a_partial_failure_suppresses_the_over_budget_verdict(self) -> None:
        # An over-budget corpus that failed to shrink is a failure to *act*, not
        # proof that policy cannot shrink it. Reporting both would send a human
        # to relax retention when the real problem is the failed mutation.
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"e" * 8192}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        with unittest.mock.patch.object(
            retention, "remove_run_directory", return_value="I/O error"
        ):
            result = self._run(apply=True, budget_bytes=1024)

        codes = [finding.code for finding in result.health]
        self.assertIn(maintenance.HEALTH_PARTIAL_FAILURE, codes)
        self.assertNotIn(maintenance.HEALTH_PROTECTED_OVER_BUDGET, codes)


class ProtectedOverBudgetTests(MaintenanceTestBase):
    """When only protected data is left, nothing goes and a human is told."""

    def test_protected_data_over_budget_raises_a_strict_finding(self) -> None:
        locked = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"L" * 16384}
        )
        self._write_lock(pid=os.getpid())

        result = self._run(apply=True, budget_bytes=1024)

        self.assertFalse(result.ok)
        findings = {finding.code: finding for finding in result.health}
        self.assertIn(maintenance.HEALTH_PROTECTED_OVER_BUDGET, findings)
        self.assertTrue(findings[maintenance.HEALTH_PROTECTED_OVER_BUDGET].strict)
        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.removed_body_count, 0)
        self.assertTrue((locked / "turn-0001-session-1" / "stderr.log").is_file())

    def test_a_corpus_policy_can_bring_under_budget_raises_nothing(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"R" * 16384}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, budget_bytes=8192)

        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertTrue(result.ok)
        self.assertEqual(result.health, ())

    def test_a_dry_run_projects_the_over_budget_verdict_too(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"P" * 16384}
        )
        self._write_lock(pid=os.getpid())

        result = self._run(apply=False, budget_bytes=1024)

        self.assertFalse(result.ok)
        self.assertIn(
            maintenance.HEALTH_PROTECTED_OVER_BUDGET,
            [finding.code for finding in result.health],
        )


class ConcurrencyTests(MaintenanceTestBase):
    """Two passes never interleave; the second declines rather than racing."""

    def test_a_held_claim_makes_the_second_pass_decline_without_mutating(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        claim = maintenance._maintenance_claim(
            maintenance.lock_path(self.state_root), wait_seconds=0.0
        )
        with claim as held:
            self.assertTrue(held)
            result = self._run(apply=True, lock_wait_seconds=0.05)

        self.assertEqual(result.outcome, maintenance.OUTCOME_LOCK_UNAVAILABLE)
        self.assertFalse(result.applied)
        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())

    def test_the_claim_is_released_when_the_holder_finishes(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        first = self._run(apply=True)
        second = self._run(apply=True)

        self.assertEqual(first.outcome, maintenance.OUTCOME_APPLIED)
        self.assertEqual(second.outcome, maintenance.OUTCOME_APPLIED)

    def test_two_concurrent_passes_produce_exactly_one_applier(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        gate = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()
        real_revalidate = maintenance._revalidate

        def slow(*args: Any, **kwargs: Any) -> Any:
            # Widen the mutation window so the loser cannot simply arrive late.
            gate.wait(timeout=10)
            return real_revalidate(*args, **kwargs)

        def worker() -> None:
            result = self._run(apply=True, lock_wait_seconds=0.05, record=False)
            with guard:
                outcomes.append(result.outcome)
            if result.outcome != maintenance.OUTCOME_APPLIED:
                gate.wait(timeout=10)

        with unittest.mock.patch.object(maintenance, "_revalidate", slow):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes.count(maintenance.OUTCOME_APPLIED), 1)
        self.assertEqual(outcomes.count(maintenance.OUTCOME_LOCK_UNAVAILABLE), 1)

    def test_waiting_pass_remeasures_pressure_after_acquiring_claim(self) -> None:
        """The pass that waited its turn deletes nothing if the wait fixed it.

        Pressure is what authorizes a pass to delete, and it is read before the
        claim is even requested. A pass that then blocks behind another pass is
        holding a fact that the other pass exists to falsify, so it reads it
        again once the claim is its own.
        """
        # Over budget when the waiting pass measures; under it once the pass
        # ahead reclaims the other plan's expired run.
        half = b"x" * (1 << 20)
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": half}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        other_plan = self._make_plan("other")
        relieving = self._write_run(
            RUN_ANCIENT,
            finished_at=_stamp(300),
            plan_dir=other_plan,
            bodies={"stderr.log": half},
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1), plan_dir=other_plan)
        # Both bodies together are over; either one alone is under.
        budget = 3 * (1 << 19)

        measured = threading.Event()
        real_measure = maintenance.measure_corpus

        def measure_then_signal(*args: Any, **kwargs: Any) -> Any:
            usage = real_measure(*args, **kwargs)
            # The pre-claim reading is done; the waiting pass is now parked on
            # the claim this thread holds, which is the exact race to reproduce.
            measured.set()
            return usage

        results: list[maintenance.MaintenanceResult] = []

        def worker() -> None:
            results.append(
                maintenance.run_maintenance(
                    self.state_root,
                    apply=True,
                    force=False,
                    now=NOW,
                    budget_bytes=budget,
                    min_free_fraction=NEVER_LOW,
                    lock_wait_seconds=30.0,
                    record=False,
                )
            )

        claim = maintenance._maintenance_claim(
            maintenance.lock_path(self.state_root), wait_seconds=0.0
        )
        with claim as held:
            self.assertTrue(held)
            with unittest.mock.patch.object(
                maintenance, "measure_corpus", measure_then_signal
            ):
                thread = threading.Thread(target=worker)
                thread.start()
                self.assertTrue(measured.wait(timeout=10))
                # Stand in for the pass ahead: relieve the pressure, then hand
                # the claim over.
                self.assertEqual(retention.remove_run_directory(relieving), "")
        thread.join(timeout=20)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.outcome, maintenance.OUTCOME_PRESSURE_RELIEVED)
        self.assertFalse(result.applied)
        self.assertTrue(result.ok)
        self.assertEqual(result.removed_run_ids, ())
        self.assertIsNone(result.plan)
        # Both readings are kept: the one that admitted the pass, and the one
        # that sent it home.
        self.assertTrue(result.usage.over_budget)
        self.assertIsNotNone(result.usage_at_claim)
        assert result.usage_at_claim is not None
        self.assertFalse(result.usage_at_claim.triggered)
        self.assertEqual(result.trigger, maintenance.TRIGGER_NONE)
        self.assertTrue(expired.is_dir())

        # And the run was genuinely reclaimable: only the remeasure spared it.
        forced = self._run(apply=True, budget_bytes=budget)
        self.assertEqual(forced.removed_run_ids, (RUN_OLD,))

    def test_a_relieved_pass_still_acts_when_force_was_requested(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, force=True)

        self.assertEqual(result.outcome, maintenance.OUTCOME_APPLIED)
        self.assertEqual(result.removed_run_ids, (RUN_OLD,))
        self.assertFalse(expired.exists())


class DeadlineTests(MaintenanceTestBase):
    """A bounded pass stops cleanly and says what it left behind."""

    def test_an_expired_deadline_stops_work_and_reports_it(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        result = self._run(apply=True, max_runtime_seconds=0.0)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        findings = {finding.code: finding for finding in result.health}
        self.assertIn(maintenance.HEALTH_DEADLINE_EXCEEDED, findings)
        # Running out of time is a schedule fact, not a corpus fault.
        self.assertFalse(findings[maintenance.HEALTH_DEADLINE_EXCEEDED].strict)
        self.assertTrue(result.ok)


class ManifestTests(MaintenanceTestBase):
    """The structured result survives the process that produced it."""

    def test_the_manifest_records_the_last_run_and_a_bounded_history(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        for _ in range(maintenance.MANIFEST_HISTORY_LIMIT + 3):
            self._run(apply=True)

        payload = maintenance.read_manifest(self.state_root)

        self.assertEqual(payload["schema_version"], maintenance.MANIFEST_SCHEMA_VERSION)
        self.assertEqual(payload["last_run"]["outcome"], maintenance.OUTCOME_APPLIED)
        self.assertEqual(len(payload["history"]), maintenance.MANIFEST_HISTORY_LIMIT)

    def test_the_manifest_carries_reclaimed_bytes_and_failures(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"m" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        self._run(apply=True)
        last_run = maintenance.read_manifest(self.state_root)["last_run"]

        self.assertGreaterEqual(last_run["reclaimed_bytes"], 4096)
        self.assertEqual(last_run["removed_run_count"], 1)
        self.assertEqual(last_run["failures"], [])
        self.assertTrue(last_run["ok"])

    def test_a_manifest_that_cannot_be_written_fails_the_pass(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        def refuse(path: Path, payload: Any, **kwargs: Any) -> None:
            if path == maintenance.manifest_path(self.state_root):
                raise OSError("Read-only file system")

        with unittest.mock.patch.object(
            maintenance.durability, "write_json_file", refuse
        ):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertEqual(result.failures[-1].kind, maintenance.FAILURE_MANIFEST_WRITE)

    def test_an_absent_manifest_reads_as_empty(self) -> None:
        self.assertEqual(maintenance.read_manifest(self.state_root), {})

    def test_the_result_serializes_to_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        payload = json.loads(json.dumps(self._run(apply=True).to_dict()))

        self.assertEqual(payload["command"], "maintenance run")
        self.assertIn("usage", payload)
        self.assertIn("plan", payload)


class LaunchdJobTests(MaintenanceTestBase):
    """The scheduled job is ASL's own: bounded, absolute, and removable."""

    def setUp(self) -> None:
        super().setUp()
        self.home = self.tmp_path / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.executable = self.tmp_path / "asl"
        self.executable.write_text("#!/bin/sh\n", encoding="utf-8")
        self.executable.chmod(0o755)
        self.calls: list[list[str]] = []

    def _launchctl(self, returncode: int = 0) -> Any:
        def fake(argv: list[str], **kwargs: Any) -> Any:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode, "", "")

        return fake

    def _install(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("executable", str(self.executable))
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("state_dir", self.state_root)
        kwargs.setdefault("platform", "darwin")
        kwargs.setdefault("launchctl", self._launchctl())
        return maintenance.install_job(**kwargs)

    def test_the_rendered_job_is_daily_bounded_and_never_runs_at_load(self) -> None:
        document = maintenance.render_plist(
            str(self.executable), state_dir=self.state_root
        )
        payload = json.loads(json.dumps(plistlib.loads(document)))

        self.assertEqual(payload["Label"], maintenance.JOB_LABEL)
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertFalse(payload["RunAtLoad"])
        argv = payload["ProgramArguments"]
        self.assertEqual(argv[0], str(self.executable))
        self.assertIn("--apply", argv)
        self.assertIn("--json", argv)
        self.assertIn("--max-runtime-seconds", argv)
        self.assertIn("--state-dir", argv)

    def test_installing_writes_the_definition_and_bootstraps_it(self) -> None:
        payload = self._install()

        target = maintenance.plist_path(self.home)
        self.assertTrue(target.is_file())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["bootstrapped"])
        self.assertEqual(payload["plist_path"], str(target))
        self.assertEqual([call[1] for call in self.calls], ["bootout", "bootstrap"])

    def test_installing_is_idempotent_and_byte_stable(self) -> None:
        self._install()
        first = maintenance.plist_path(self.home).read_bytes()
        self._install()

        self.assertEqual(maintenance.plist_path(self.home).read_bytes(), first)

    def test_no_bootstrap_writes_the_definition_without_touching_launchctl(
        self,
    ) -> None:
        payload = self._install(bootstrap=False)

        self.assertTrue(maintenance.plist_path(self.home).is_file())
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["bootstrapped"])
        self.assertEqual(self.calls, [])

    def test_a_failed_bootstrap_is_reported_rather_than_swallowed(self) -> None:
        payload = self._install(launchctl=self._launchctl(returncode=1))

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["bootstrapped"])

    def test_a_missing_executable_refuses_before_anything_is_written(self) -> None:
        with self.assertRaises(maintenance.MaintenanceError):
            self._install(executable=str(self.tmp_path / "nope"))

        self.assertFalse(maintenance.plist_path(self.home).exists())

    def test_a_platform_without_launchd_refuses_with_an_alternative(self) -> None:
        with self.assertRaises(maintenance.MaintenanceError) as caught:
            self._install(platform="linux")

        self.assertIn("maintenance run --apply", str(caught.exception))
        self.assertFalse(maintenance.plist_path(self.home).exists())

    def test_uninstalling_removes_the_job_and_keeps_every_artifact(self) -> None:
        self._install()
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)
        manifest = maintenance.manifest_path(self.state_root)
        self.assertTrue(manifest.is_file())
        self.calls.clear()

        payload = maintenance.uninstall_job(
            home=self.home,
            state_dir=self.state_root,
            platform="darwin",
            launchctl=self._launchctl(),
        )

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["plist_removed"])
        self.assertTrue(payload["booted_out"])
        self.assertFalse(maintenance.plist_path(self.home).exists())
        self.assertTrue(manifest.is_file())
        self.assertTrue(self._state_dir().is_dir())

    def test_uninstalling_a_job_that_is_not_installed_is_not_an_error(self) -> None:
        payload = maintenance.uninstall_job(
            home=self.home,
            state_dir=self.state_root,
            platform="darwin",
            launchctl=self._launchctl(),
        )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["plist_removed"])


class JobStatusTests(LaunchdJobTests):
    """Install verification reports presence, last run, bytes, and failures."""

    def _status(self, **kwargs: Any) -> maintenance.JobStatus:
        kwargs.setdefault("home", self.home)
        kwargs.setdefault("state_dir", self.state_root)
        kwargs.setdefault("executable", str(self.executable))
        kwargs.setdefault("platform", "darwin")
        kwargs.setdefault("launchctl", self._launchctl())
        return maintenance.job_status(**kwargs)

    def test_status_reports_an_installed_current_and_loaded_job(self) -> None:
        self._install()

        status = self._status()

        self.assertTrue(status.plist_present)
        self.assertTrue(status.plist_current)
        self.assertTrue(status.loaded)
        self.assertEqual(status.label, maintenance.JOB_LABEL)

    def test_status_reports_a_definition_that_no_longer_matches(self) -> None:
        self._install(hour=1, minute=0)

        status = self._status()

        self.assertTrue(status.plist_present)
        self.assertFalse(status.plist_current)

    def test_status_reports_an_absent_job_without_failing(self) -> None:
        status = self._status(probe=False)

        self.assertFalse(status.plist_present)
        self.assertFalse(status.loaded)
        self.assertTrue(status.ok)

    def test_status_reports_the_last_run_bytes_and_failures(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"s" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)

        status = self._status(probe=False)

        self.assertEqual(status.last_run["removed_run_count"], 1)
        self.assertGreaterEqual(status.last_run["reclaimed_bytes"], 4096)
        self.assertEqual(status.last_run["failures"], [])
        self.assertTrue(status.ok)

    def test_status_fails_when_the_last_recorded_pass_failed(self) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        with unittest.mock.patch.object(
            retention, "remove_run_directory", return_value="I/O error"
        ):
            self._run(apply=True)

        status = self._status(probe=False)

        self.assertFalse(status.ok)
        self.assertEqual(len(status.last_run["failures"]), 1)

    def test_status_says_so_on_a_platform_that_cannot_schedule(self) -> None:
        status = self._status(platform="linux")

        self.assertFalse(status.supported)
        self.assertIn("no launchd", status.load_detail)


class CommandTests(MaintenanceTestBase):
    """The CLI surface exits on the same verdicts the library reports."""

    def _args(self, **fields: Any) -> argparse.Namespace:
        base: dict[str, Any] = {
            "state_dir": str(self.state_root),
            "json": True,
            "apply": False,
            "force": True,
            "max_age_days": retention.DEFAULT_MAX_AGE_DAYS,
            "max_runtime_seconds": maintenance.DEFAULT_MAX_RUNTIME_SECONDS,
        }
        base.update(fields)
        return argparse.Namespace(**base)

    def _capture(self, handler: Any, args: argparse.Namespace) -> tuple[int, Any]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = handler(args)
        return code, json.loads(buffer.getvalue())

    def test_run_exits_zero_and_prints_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))

        code, payload = self._capture(maintenance.run_from_args, self._args())

        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["command"], "maintenance run")

    def test_run_exits_non_zero_on_a_strict_health_finding(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"h" * 8192}
        )
        self._write_lock(pid=os.getpid())

        with unittest.mock.patch.object(maintenance, "CORPUS_BUDGET_BYTES", 1024):
            code, payload = self._capture(
                maintenance.run_from_args, self._args(apply=True)
            )

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_run_renders_a_human_summary_without_json(self) -> None:
        self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"t" * 4096}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = maintenance.run_from_args(self._args(json=False))

        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Reclaimable:", text)
        self.assertIn("--apply", text)

    def test_run_reports_an_error_as_json_rather_than_raising(self) -> None:
        with unittest.mock.patch.object(
            maintenance,
            "measure_corpus",
            side_effect=maintenance.MaintenanceError("volume vanished"),
        ):
            code, payload = self._capture(maintenance.run_from_args, self._args())

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("volume vanished", payload["error"])

    def test_status_renders_a_human_summary_without_json(self) -> None:
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        self._run(apply=True)
        buffer = io.StringIO()

        with redirect_stdout(buffer):
            code = maintenance.status_from_args(
                self._args(json=False, executable=None, no_probe=True)
            )

        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(maintenance.JOB_LABEL, text)
        self.assertIn("Failures: 0", text)

    def test_install_reports_a_refusal_as_json(self) -> None:
        code, payload = self._capture(
            maintenance.install_from_args,
            self._args(
                executable=str(self.tmp_path / "absent"),
                hour=3,
                minute=30,
                no_bootstrap=True,
            ),
        )

        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["command"], "maintenance install")


if __name__ == "__main__":
    unittest.main()
