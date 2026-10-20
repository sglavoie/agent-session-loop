"""Under-the-claim revalidation and fence tests for storage maintenance.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import unittest
import unittest.mock
from pathlib import Path
from typing import Any

from agent_session_loop import (
    maintenance_bodies,
    maintenance_mutator,
    maintenance_plan,
    maintenance_policy,
    maintenance_run,
    retention_projection,
)
from tests.asl_maintenance_support import (
    RUN_NEWEST,
    RUN_OLD,
    MaintenanceTestBase,
    _stamp,
)


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
        real = retention_projection.live_run_keys

        def racing(state_dir: Any = None) -> frozenset[tuple[str, str]]:
            calls.append(1)
            if len(calls) == 1:
                return real(state_dir)
            # The revalidation read, and every read after it, sees the run come
            # alive between planning and mutation.
            return frozenset({(plan_state, RUN_OLD)})

        with unittest.mock.patch.object(retention_projection, "live_run_keys", racing):
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

        real = maintenance_plan._revalidate

        def locking(*args: Any, **kwargs: Any) -> Any:
            self._write_lock(pid=os.getpid())
            return real(*args, **kwargs)

        with unittest.mock.patch.object(maintenance_run, "_revalidate", locking):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertIn("plan lock is active", drop["reason"])
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_REVALIDATION)

    def test_pin_acquired_after_initial_revalidation_blocks_removal(self) -> None:
        """A pin that lands between revalidation and removal still protects."""
        expired = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": b"p" * 512}
        )
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        real = maintenance_plan._revalidate

        def revalidate_then_pin(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            # The issue is opened after the plan has been revalidated and
            # cleared for removal. Nothing but a reading taken at the moment of
            # deletion can see it, because the assurance writer never took the
            # maintenance claim and was free to land here.
            self._pin_run(RUN_OLD)
            return approved

        with unittest.mock.patch.object(
            maintenance_run, "_revalidate", revalidate_then_pin
        ):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertEqual(result.reclaimed_bytes, 0)
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["run_id"], RUN_OLD)
        self.assertEqual(drop["lane"], maintenance_policy.LANE_RECLAIM)
        # Recorded as a mutation-time drop: the plan cleared this run and the
        # fence in front of the removal itself is what saved it.
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_MUTATION)
        self.assertIn("assurance pin", drop["reason"])

    def test_a_lock_acquired_after_initial_revalidation_blocks_removal(self) -> None:
        expired = self._write_run(RUN_OLD, finished_at=_stamp(200))
        self._write_run(RUN_NEWEST, finished_at=_stamp(1))
        real = maintenance_plan._revalidate

        def revalidate_then_lock(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            self._write_lock(pid=os.getpid())
            return approved

        with unittest.mock.patch.object(
            maintenance_run, "_revalidate", revalidate_then_lock
        ):
            result = self._run(apply=True)

        self.assertEqual(result.removed_run_ids, ())
        self.assertTrue(expired.is_dir())
        self.assertEqual(len(result.revalidation_drops), 1)
        drop = result.revalidation_drops[0]
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_MUTATION)
        self.assertIn("plan lock is active", drop["reason"])

    def test_a_pin_acquired_after_initial_revalidation_blocks_compaction(self) -> None:
        """The compaction lane is fenced too: compressing unlinks the original."""
        payload = b"repeat me " * 4096
        run_dir = self._write_run(
            RUN_OLD, finished_at=_stamp(200), bodies={"stderr.log": payload}
        )
        real = maintenance_plan._revalidate

        def revalidate_then_pin(*args: Any, **kwargs: Any) -> Any:
            approved = real(*args, **kwargs)
            self._pin_run(RUN_OLD)
            return approved

        with unittest.mock.patch.object(
            maintenance_run, "_revalidate", revalidate_then_pin
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
        self.assertEqual(drop["lane"], maintenance_policy.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_MUTATION)
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
        real = maintenance_bodies._stage_compressed

        def stage_then_pin(source: Path, target: Path) -> tuple[Path, str, int]:
            staged = real(source, target)
            self._pin_run(RUN_OLD)
            return staged

        with unittest.mock.patch.object(
            maintenance_mutator, "_stage_compressed", stage_then_pin
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
        self.assertEqual(drop["lane"], maintenance_policy.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_MUTATION)
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
        real = maintenance_bodies._write_ledger

        def publish_then_pin(source: Path, ledger: dict[str, Any]) -> None:
            real(source, ledger)
            self._pin_run(RUN_OLD)

        with unittest.mock.patch.object(
            maintenance_mutator, "_write_ledger", publish_then_pin
        ):
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
            result.revalidation_drops[0]["stage"], maintenance_policy.STAGE_MUTATION
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
        real = maintenance_bodies._write_ledger
        pinned: list[int] = []

        def publish_then_pin(source: Path, ledger: dict[str, Any]) -> None:
            real(source, ledger)
            # The compression ledger is written first and must be allowed
            # through; the pin lands against the removal tombstone.
            if ledger.get("removed") and not pinned:
                pinned.append(1)
                self._pin_run(RUN_OLD)

        with unittest.mock.patch.object(
            maintenance_mutator, "_write_ledger", publish_then_pin
        ):
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
        self.assertEqual(drop["lane"], maintenance_policy.LANE_COMPACTION)
        self.assertEqual(drop["stage"], maintenance_policy.STAGE_MUTATION)
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
        maintenance_bodies._compress_file(turn / "stdout.log", turn / "stdout.log.gz")
        (turn / "stdout.log").unlink()
        real = maintenance_bodies._digest_file

        def digest_then_pin(path: Path) -> tuple[str, int]:
            digested = real(path)
            self._pin_run(RUN_OLD)
            return digested

        with unittest.mock.patch.object(
            maintenance_mutator, "_digest_file", digest_then_pin
        ):
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
            result.revalidation_drops[0]["stage"], maintenance_policy.STAGE_MUTATION
        )


if __name__ == "__main__":
    unittest.main()
