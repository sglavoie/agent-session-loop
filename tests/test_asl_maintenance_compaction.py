"""Log-body compaction and partial-failure tests for storage maintenance.

Split from tests/test_asl_maintenance.py."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import unittest
import unittest.mock
from pathlib import Path

from agent_session_loop import (
    durability,
    maintenance_bodies,
    maintenance_models,
    maintenance_mutator,
    maintenance_plan,
    maintenance_policy,
    retention,
)
from tests.asl_maintenance_support import (
    NEVER_LOW,
    NOW,
    RUN_NEWEST,
    RUN_OLD,
    MaintenanceTestBase,
    _stamp,
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
        for name in maintenance_policy.LOG_BODY_NAMES:
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

    def test_the_dry_run_judges_the_budget_after_compression_like_apply(self) -> None:
        # ~440 KB raw, a couple of KB once gzipped: over the budget only raw.
        run_dir = self._newest_only(
            age_days=200, bodies={"stdout.log": b"compressible log line\n" * 20000}
        )
        turn = run_dir / "turn-0001-session-1"

        dry = self._run(apply=False, log_body_budget_bytes=100_000)

        assert dry.plan is not None
        (entry,) = dry.plan.compactions
        (action,) = entry.actions
        self.assertEqual(action.action, "compress")
        self.assertEqual(entry.removable_bytes, 0)
        self.assertLess(action.stored_bytes, 100_000)
        self.assertEqual(entry.body_bytes_after, action.stored_bytes)

        applied = self._run(apply=True, log_body_budget_bytes=100_000)

        self.assertEqual(applied.removed_body_count, 0)
        self.assertEqual(applied.compressed_body_count, 1)
        self.assertEqual((turn / "stdout.log.gz").stat().st_size, action.stored_bytes)

    def test_a_removed_body_leaves_path_bytes_digest_and_reason_behind(self) -> None:
        payload = os.urandom(40_000)
        run_dir = self._newest_only(age_days=200, bodies={"stdout.log": payload})
        turn = run_dir / "turn-0001-session-1"

        self._run(apply=True, log_body_budget_bytes=0)

        ledger = json.loads((turn / "stdout.log.body.json").read_text(encoding="utf-8"))
        self.assertTrue(ledger["removed"])
        self.assertEqual(ledger["reason"], maintenance_policy.REASON_LOG_BODY_BUDGET)
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

        for name in maintenance_policy.LOG_BODY_NAMES:
            self.assertFalse((turn / name).exists())
            self.assertFalse((turn / f"{name}.gz").exists())
            ledger = json.loads(
                (turn / f"{name}.body.json").read_text(encoding="utf-8")
            )
            self.assertTrue(ledger["removed"])
            self.assertEqual(
                ledger["reason"], maintenance_policy.REASON_LOG_BODY_BUDGET
            )
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
            plan = maintenance_plan.plan_maintenance(
                self.state_root,
                now=NOW,
                force=True,
                log_body_budget_bytes=35_000,
                usage=maintenance_models.measure_corpus(
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
        maintenance_bodies._compress_file(turn / "stderr.log", turn / "stderr.log.gz")

        result = self._run(apply=True)

        self.assertEqual(result.compressed_body_count, 1)
        self.assertFalse((turn / "stderr.log").exists())
        self.assertEqual(
            gzip.decompress((turn / "stderr.log.gz").read_bytes()), payload
        )

    def test_compression_rejects_a_staging_link_without_touching_its_target(
        self,
    ) -> None:
        source = self.tmp_path / "body.log"
        target = self.tmp_path / "body.log.gz"
        victim = self.tmp_path / "unrelated.log"
        source.write_bytes(b"body")
        victim.write_bytes(b"unrelated")
        staging = durability.staging_path(target)
        staging.symlink_to(victim)

        with self.assertRaises(OSError):
            maintenance_bodies._stage_compressed(source, target)

        self.assertEqual(b"body", source.read_bytes())
        self.assertEqual(b"unrelated", victim.read_bytes())
        self.assertFalse(target.exists())
        self.assertFalse(staging.exists())


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
        self.assertEqual(failure.kind, maintenance_policy.FAILURE_RUN_REMOVAL)
        self.assertEqual(failure.error, "Permission denied")
        self.assertGreaterEqual(failure.bytes_still_allocated, 4096)
        self.assertIn(
            maintenance_policy.HEALTH_PARTIAL_FAILURE,
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

        with unittest.mock.patch.object(
            maintenance_mutator, "_stage_compressed", refuse
        ):
            result = self._run(apply=True)

        self.assertFalse(result.ok)
        self.assertTrue((turn / "stderr.log").is_file())
        self.assertEqual(result.compressed_body_count, 0)
        self.assertEqual(
            result.failures[0].kind, maintenance_policy.FAILURE_BODY_COMPRESS
        )
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
        self.assertIn(maintenance_policy.HEALTH_PARTIAL_FAILURE, codes)
        self.assertNotIn(maintenance_policy.HEALTH_PROTECTED_OVER_BUDGET, codes)


if __name__ == "__main__":
    unittest.main()
