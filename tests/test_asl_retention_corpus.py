"""Corpus-wide retention sweep and dry-run purity of the run-retention projection."""

from __future__ import annotations

import io
import json
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agent_session_loop import (
    plans_files,
    retention,
    retention_projection,
    state,
    state_locks,
)
from tests.asl_retention_support import (
    DEAD_PID,
    NOW,
    RUN_NEW,
    RUN_OLD,
    RetentionTestBase,
    _stamp,
)

ASL_TEST_PORTABLE = True


class CorpusRetentionTests(RetentionTestBase):
    """Aggregation across every plan state directory under a state root."""

    def test_aggregates_every_plan(self) -> None:
        other_plan = self._make_plan("other-plan")
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._write_run(
            RUN_OLD, finished_at=_stamp(60), plan_dir=other_plan, log_bytes=500
        )
        self._write_run(RUN_NEW, finished_at=_stamp(2), plan_dir=other_plan)

        report = retention_projection.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(len(report.plans), 2)
        self.assertEqual(
            [entry.plan_dir for entry in report.plans],
            sorted(entry.plan_dir for entry in report.plans),
        )
        self.assertEqual(report.reclaimable_run_count, 2)
        self.assertEqual(
            report.reclaimable_bytes,
            sum(entry.reclaimable_bytes for entry in report.plans),
        )
        self.assertGreater(report.total_bytes, report.reclaimable_bytes)
        self.assertEqual(report.unresolved_state_dirs, ())
        json.dumps(report.to_dict())

    def test_state_dir_without_a_recoverable_plan_is_not_judged(self) -> None:
        orphan = self.state_root / ("0" * 64)
        orphan.mkdir(parents=True)
        (orphan / state.RUNS_DIR_NAME / RUN_OLD).mkdir(parents=True)

        report = retention_projection.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(
            report.unresolved_state_dirs, (str(plans_files.canonicalize_path(orphan)),)
        )
        self.assertEqual(report.reclaimable_bytes, 0)

    def test_state_dir_claiming_another_plan_is_not_judged(self) -> None:
        impostor = self.state_root / ("f" * 64)
        impostor.mkdir(parents=True)
        (impostor / "state.json").write_text(
            json.dumps({"plan_dir": str(plans_files.canonicalize_path(self.plan_dir))}),
            encoding="utf-8",
        )

        report = retention_projection.corpus_retention(self.state_root, now=NOW)

        self.assertEqual(
            report.unresolved_state_dirs,
            (str(plans_files.canonicalize_path(impostor)),),
        )

    def test_missing_state_root_is_empty(self) -> None:
        report = retention_projection.corpus_retention(
            self.tmp_path / "absent", now=NOW
        )

        self.assertEqual(report.plans, ())
        self.assertEqual(report.reclaimable_bytes, 0)


class DryRunPurityTests(RetentionTestBase):
    """A proven-dead same-host lock survives every scope and every mode.

    That lock is the one the ordinary read path removes, which is what made a
    dry run write and made the two scopes disagree: corpus scope reclaimed it
    in the liveness pre-pass and then judged the plan unlocked, while plan
    scope removed it and reported `plan-locked` from the same read.
    """

    def setUp(self) -> None:
        super().setUp()
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=1000)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self.lock_path = self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())

    def _lock_state(self) -> tuple[bytes, int, int]:
        info = self.lock_path.stat()
        return self.lock_path.read_bytes(), info.st_ino, info.st_size

    def _decisions(
        self, report: retention_projection.PlanRetention
    ) -> list[tuple[str, bool, str]]:
        return [(run.run_id, run.reclaimable, run.reason) for run in report.runs]

    def _corpus_report(self, **kwargs: object) -> retention_projection.PlanRetention:
        corpus = retention_projection.corpus_retention(
            self.state_root, now=NOW, **kwargs
        )
        canonical = str(plans_files.canonicalize_path(self.plan_dir))
        return next(entry for entry in corpus.plans if entry.plan_dir == canonical)

    def test_the_ordinary_read_would_have_removed_this_lock(self) -> None:
        # Without this the tests below could pass against a lock nothing
        # reclaims, which is exactly what the pre-repair suite did.
        with redirect_stderr(io.StringIO()):
            lock_info = state_locks.read_lock_info(self.plan_dir, self.state_root)

        self.assertIsNotNone(lock_info)
        self.assertFalse(self.lock_path.exists())

    def test_plan_scope_dry_run_preserves_the_lock(self) -> None:
        before = self._lock_state()

        report = retention_projection.plan_retention(
            self.plan_dir, state_dir=self.state_root, now=NOW
        )

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            {run.reason for run in report.runs},
            {retention_projection.RETAIN_PLAN_LOCKED},
        )

    def test_corpus_scope_dry_run_preserves_the_lock(self) -> None:
        before = self._lock_state()

        report = self._corpus_report()

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.reclaimable_bytes, 0)
        self.assertEqual(
            {run.reason for run in report.runs},
            {retention_projection.RETAIN_PLAN_LOCKED},
        )

    def test_the_running_scan_preserves_the_lock(self) -> None:
        before = self._lock_state()

        keys = retention_projection.live_run_keys(self.state_root)

        self.assertEqual(before, self._lock_state())
        self.assertEqual(keys, frozenset())

    def test_both_scopes_reach_the_same_decision(self) -> None:
        plan_scope = self._decisions(
            retention_projection.plan_retention(
                self.plan_dir, state_dir=self.state_root, now=NOW
            )
        )
        # Re-establish the lock so the second scope judges the same corpus as
        # the first even when the first removed it. The bytes are identical,
        # so this is a no-op against a projection that writes nothing.
        self._write_lock(pid=DEAD_PID, hostname=socket.gethostname())
        corpus_scope = self._decisions(self._corpus_report())

        self.assertEqual(plan_scope, corpus_scope)
        self.assertEqual(
            {reason for _id, _reclaimable, reason in plan_scope},
            {retention_projection.RETAIN_PLAN_LOCKED},
        )
        self.assertTrue(self.lock_path.exists())

    def test_apply_removes_run_directories_and_never_the_lock(self) -> None:
        before = self._lock_state()
        sweepable = self._make_plan("sweepable")
        old = self._write_run(
            RUN_OLD, finished_at=_stamp(45), plan_dir=sweepable, log_bytes=1000
        )
        self._write_run(RUN_NEW, finished_at=_stamp(1), plan_dir=sweepable)

        report = retention.run_gc(state_dir=self.state_root, apply=True, now=NOW)

        self.assertEqual(before, self._lock_state())
        self.assertEqual(report.removed_run_count, 1)
        self.assertFalse(old.exists())
        self.assertTrue(
            (self.plan_state / state.RUNS_DIR_NAME / RUN_OLD).is_dir(),
            "a locked plan keeps every run under --apply",
        )


class ArchiveReportTests(RetentionTestBase):
    """gc states the Workbench archive's size without judging or deleting it."""

    def _archive(self, archive_id: str, *, days_ago: float, size: int) -> None:
        entry = self.state_root / "archive" / archive_id
        (entry / "plan").mkdir(parents=True)
        (entry / "plan" / "blob.bin").write_bytes(b"x" * size)
        (entry / "archive.json").write_text(
            json.dumps(
                {
                    "archive_id": archive_id,
                    "original_plan_dir": str(self.tmp_path / archive_id),
                    "archived_at": _stamp(days_ago),
                    "expires_at": _stamp(days_ago - 30),
                }
            ),
            encoding="utf-8",
        )

    def _gc(self, *, apply: bool = False) -> retention.GCReport:
        return retention.run_gc(
            state_dir=self.state_root,
            now=NOW,
            apply=apply,
            measure_archive=True,
            registry_path=self.tmp_path / "absent-registry.json",
        )

    def test_archive_bytes_and_expired_entries_are_reported_not_reclaimed(
        self,
    ) -> None:
        self._write_run(RUN_OLD, finished_at=_stamp(45), log_bytes=100)
        self._write_run(RUN_NEW, finished_at=_stamp(1))
        self._archive("old-entry", days_ago=90, size=5000)
        self._archive("new-entry", days_ago=1, size=7000)

        report = self._gc(apply=True)

        archive = report.to_dict()["archive"]
        self.assertFalse(archive["managed_by_gc"])
        self.assertEqual(2, archive["entry_count"])
        self.assertGreaterEqual(archive["bytes"], 12000)
        self.assertEqual(30, archive["retention_days"])
        self.assertEqual(1, archive["past_retention_count"])
        self.assertGreaterEqual(archive["past_retention_bytes"], 5000)
        self.assertLess(archive["past_retention_bytes"], 7000)
        self.assertIn("Workbench Archive view", archive["purge_hint"])
        # --apply reclaimed the old run and left every archive entry alone.
        self.assertEqual(1, report.removed_run_count)
        self.assertTrue((self.state_root / "archive" / "old-entry").is_dir())
        self.assertTrue((self.state_root / "archive" / "new-entry").is_dir())

        out = io.StringIO()
        with redirect_stdout(out):
            retention._print_report(report)
        text = out.getvalue()
        self.assertIn("measured in run directories", text)
        self.assertIn("Archive (not managed by gc): 2 entry(ies)", text)
        self.assertIn("1 past the 30-day retention window", text)
        self.assertIn("gc never deletes archive entries", text)

    def test_missing_archive_reads_as_empty(self) -> None:
        archive = self._gc().to_dict()["archive"]
        self.assertEqual(0, archive["entry_count"])
        self.assertEqual(0, archive["bytes"])
        self.assertEqual(0, archive["past_retention_count"])

    def test_plan_scope_and_unmeasured_sweeps_carry_no_archive(self) -> None:
        self._archive("entry", days_ago=90, size=10)
        self.assertIsNone(
            retention.run_gc(state_dir=self.state_root, now=NOW).to_dict()["archive"]
        )
        self.assertIsNone(
            retention.run_gc(
                self.plan_dir, state_dir=self.state_root, now=NOW, measure_archive=True
            ).archive
        )


if __name__ == "__main__":
    unittest.main()
