"""Manifest, archive, transaction, and authored plan tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    authoring_manifest,
    authoring_transaction,
    contracts,
    plans_content_hash,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_readiness,
    plans_session_ids,
    plans_snapshot,
    plans_transaction,
    workspaces,
)
from tests.asl_authoring_support import PlanAuthoringBase


class PlanManifestTransactionTests(PlanAuthoringBase):
    def test_render_manifest_includes_readiness_and_table(self) -> None:
        rendered = authoring_manifest.render_manifest(self._manifest_spec())
        self.assertIn("**Readiness**: draft", rendered)
        self.assertIn("**Readiness SHA-256**: `" + "d" * 64 + "`", rendered)
        self.assertIn("**Sessions**: 2", rendered)
        self.assertIn("| 1 | First | pending | session-1.md |", rendered)
        self.assertIn("/session-run /plans/out/", rendered)
        readiness = plans_readiness.parse_readiness_metadata(rendered)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_render_manifest_includes_registry_only_when_declared(self) -> None:
        registry = workspaces.RepositoryRegistry(
            (
                workspaces.Repository(
                    "SVC", "tundra_svc", "git@github.com:tundra/SVC.git"
                ),
                workspaces.Repository(
                    "OAK", "tundra_OAK", "https://github.com/tundra/OAK.git"
                ),
            )
        )
        rendered = authoring_manifest.render_manifest(
            dataclasses.replace(self._manifest_spec(), registry=registry)
        )

        self.assertIn("## Repositories", rendered)
        self.assertIn("| Alias | Path | Origin |", rendered)
        self.assertLess(rendered.index("| SVC |"), rendered.index("| OAK |"))
        self.assertIn("| SVC | tundra_svc | github.com/tundra/SVC |", rendered)
        self.assertNotIn(
            "## Repositories", authoring_manifest.render_manifest(self._manifest_spec())
        )

    def test_add_manifest_session_row_appends(self) -> None:
        rendered = authoring_manifest.render_manifest(self._manifest_spec())
        updated = authoring_manifest.add_manifest_session_row(
            rendered,
            authoring_manifest.ManifestSession(
                session_id=plans_session_ids.parse_session_id("3"), title="Third"
            ),
        )
        self.assertIn("| 3 | Third | pending | session-3.md |", updated)
        self.assertLess(updated.index("| 2 | Second"), updated.index("| 3 | Third"))

    def test_add_manifest_session_row_without_table_raises(self) -> None:
        with self.assertRaises(plans_errors.PlanError):
            authoring_manifest.add_manifest_session_row(
                "# Plan\n\nNo table here.\n",
                authoring_manifest.ManifestSession(
                    session_id=plans_session_ids.parse_session_id("1"), title="First"
                ),
            )

    def test_archive_dir_name_is_deterministic(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=UTC)
        self.assertEqual(
            "session-2.1-20260710T123045Z",
            authoring_transaction.archive_dir_name("2.1", now),
        )

    def test_create_archive_dir_is_collision_safe(self) -> None:
        now = datetime(2026, 7, 10, 12, 30, 45, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmp:
            first = authoring_transaction.create_archive_dir(tmp, "2", now)
            second = authoring_transaction.create_archive_dir(tmp, "2", now)
            self.assertEqual(authoring_transaction.ARCHIVE_ROOT_NAME, first.parent.name)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
            self.assertTrue(second.is_dir())

    def test_transaction_commits_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            tx = authoring_transaction.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            committed = tx.commit()

            self.assertEqual(2, len(committed))
            self.assertEqual("ctx", (plan_dir / "context.md").read_text())
            self.assertEqual("man", (plan_dir / "manifest.md").read_text())
            leftovers = [p.name for p in plan_dir.iterdir() if p.name.startswith(".")]
            self.assertEqual([], leftovers)

    def test_transaction_failed_write_leaves_no_partial_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "blocker").write_text("i am a file")
            tx = authoring_transaction.PlanTransaction(plan_dir)
            tx.stage("context.md", "ctx")
            tx.stage("manifest.md", "man")
            # A staged path whose parent is a regular file forces a write failure.
            tx.stage("blocker/session-1.md", "nope")

            with self.assertRaises(plans_errors.PlanError):
                tx.commit()

            self.assertFalse((plan_dir / "context.md").exists())
            self.assertFalse((plan_dir / "manifest.md").exists())
            self.assertEqual(["blocker"], sorted(p.name for p in plan_dir.iterdir()))

    def test_transaction_overwrite_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            (plan_dir / "context.md").write_text("original")
            tx = authoring_transaction.PlanTransaction(plan_dir)
            tx.stage("context.md", "updated")
            tx.commit()
            self.assertEqual("updated", (plan_dir / "context.md").read_text())

    def test_transaction_recovery_waits_for_in_flight_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            names = ("a.md", "b.md", "c.md", "d.md", "e.md")
            for name in names:
                (plan_dir / name).write_text("OLD\n", encoding="utf-8")

            started = threading.Event()
            reader_results: list[bool] = []
            reader_errors: list[BaseException] = []
            original_fsync_directory = plans_files._fsync_directory
            calls = 0

            def slow_fsync_directory(path: Path) -> None:
                nonlocal calls
                original_fsync_directory(path)
                calls += 1
                if calls == 3:
                    started.set()
                    time.sleep(0.1)

            def recover() -> None:
                if not started.wait(5):
                    reader_errors.append(AssertionError("commit did not start"))
                    return
                try:
                    reader_results.append(
                        plans_transaction.recover_pending_plan_transaction(plan_dir)
                    )
                except BaseException as error:
                    reader_errors.append(error)

            transaction = authoring_transaction.PlanTransaction(plan_dir)
            for name in names:
                transaction.stage(name, "NEW\n")
            reader = threading.Thread(target=recover)
            with mock.patch.object(
                plans_files, "_fsync_directory", side_effect=slow_fsync_directory
            ):
                reader.start()
                committed = transaction.commit()
                reader.join(timeout=5)

            self.assertFalse(reader.is_alive())
            self.assertEqual([], reader_errors)
            self.assertEqual([False], reader_results)
            self.assertEqual(len(names), len(committed))
            self.assertEqual(
                {"NEW"},
                {
                    (plan_dir / name).read_text(encoding="utf-8").strip()
                    for name in names
                },
            )

    def test_transaction_recovery_times_out_with_holder_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            transaction_dir = plan_dir / plans_transaction.PLAN_TRANSACTION_DIR_NAME
            transaction_dir.mkdir()
            (
                transaction_dir / plans_transaction.PLAN_TRANSACTION_JOURNAL_NAME
            ).write_text('{"version":1,"entries":[]}\n', encoding="utf-8")
            holder_ready = threading.Event()
            release_holder = threading.Event()

            def hold_transaction_lock() -> None:
                with plans_transaction._plan_transaction_mutex(transaction_dir):
                    holder_ready.set()
                    release_holder.wait(5)

            holder = threading.Thread(
                target=hold_transaction_lock, name="transaction-test-holder"
            )
            holder.start()
            self.assertTrue(holder_ready.wait(5))
            started_at = time.monotonic()
            try:
                with (
                    mock.patch.object(
                        plans_transaction, "PLAN_TRANSACTION_LOCK_WAIT_SECONDS", 0.1
                    ),
                    self.assertRaisesRegex(
                        plans_errors.PlanError,
                        r"timed out after 0\.1s waiting for plan transaction lock "
                        r"at .*transaction\.lock; holder pid \d+ on .* "
                        r"thread 'transaction-test-holder'",
                    ),
                ):
                    plans_metadata.load_plan_metadata(plan_dir)
            finally:
                release_holder.set()
                holder.join(timeout=5)

            self.assertFalse(holder.is_alive())
            self.assertLess(time.monotonic() - started_at, 1.0)

    def test_refresh_readiness_matches_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            metadata = authoring_transaction.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            self.assertEqual("draft", metadata.state)
            self.assertEqual(
                plans_content_hash.stable_readiness_sha256(plan_dir),
                metadata.readiness_sha256,
            )
            on_disk = plans_readiness.read_readiness_metadata(plan_dir)
            self.assertEqual(metadata, on_disk)

    def test_authored_plan_is_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = self._author_plan(Path(tmp))
            authoring_transaction.refresh_readiness_metadata(
                plan_dir, checked_at="2026-07-10T12:00:00+00:00"
            )
            snapshot = plans_snapshot.inspect_plan(plan_dir)
            self.assertEqual(("1", "2"), snapshot.session_ids)
            self.assertEqual({"1": "pending", "2": "pending"}, snapshot.status_by_id)
            for session in snapshot.sessions:
                contract = contracts.read_completion_contract(
                    plan_dir, session.session_id
                )
                self.assertIsNotNone(contract)

    def test_registry_order_changes_content_and_readiness_hashes(self) -> None:
        repositories = (
            workspaces.Repository("SVC", "svc", "https://example.com/svc.git"),
            workspaces.Repository("OAK", "oak", "https://example.com/oak.git"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            left = self._author_plan(Path(tmp) / "left")
            right = self._author_plan(Path(tmp) / "right")
            left_spec = dataclasses.replace(
                self._manifest_spec(),
                registry=workspaces.RepositoryRegistry(repositories),
            )
            right_spec = dataclasses.replace(
                self._manifest_spec(),
                registry=workspaces.RepositoryRegistry(tuple(reversed(repositories))),
            )
            (left / "manifest.md").write_text(
                authoring_manifest.render_manifest(left_spec), encoding="utf-8"
            )
            (right / "manifest.md").write_text(
                authoring_manifest.render_manifest(right_spec), encoding="utf-8"
            )

            self.assertNotEqual(
                plans_content_hash.plan_content_sha256(left),
                plans_content_hash.plan_content_sha256(right),
            )
            self.assertNotEqual(
                plans_content_hash.stable_readiness_sha256(left),
                plans_content_hash.stable_readiness_sha256(right),
            )

    def test_authoring_is_deterministic(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first_tmp,
            tempfile.TemporaryDirectory() as second_tmp,
        ):
            first_dir = self._author_plan(Path(first_tmp))
            second_dir = self._author_plan(Path(second_tmp))
            for name in (
                "manifest.md",
                "context.md",
                "progress.md",
                "session-1.md",
                "session-2.md",
            ):
                self.assertEqual(
                    (first_dir / name).read_text(),
                    (second_dir / name).read_text(),
                    name,
                )


if __name__ == "__main__":
    unittest.main()
