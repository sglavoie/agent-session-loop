"""LockRepositoryClaimTests and ResolvePlanReferenceTests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    durability,
    events,
    plans_files,
    state,
    state_locks,
    state_plan_refs,
)
from tests.support import (
    write_plan,
)


class LockRepositoryClaimTests(unittest.TestCase):
    """The registry a lock publishes, and the window in which it publishes it."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.plan_dir = root / "plan"
        self.state_dir = root / "state"
        write_plan(self.plan_dir, ("pending",))
        self.lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)

    def read_payload(self) -> dict[str, object]:
        return json.loads(self.lock_path.read_text(encoding="utf-8"))

    def test_lock_publishes_a_current_version_sorted_repository_claim(
        self,
    ) -> None:
        lock_info = state_locks.acquire_lock(
            self.plan_dir,
            self.state_dir,
            repository_roots=("/w/zebra", "/w/apple", "/w/zebra"),
        )
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        payload = self.read_payload()
        # 3 adds `wakeup_endpoint` to 2's repository claim; 4 adds
        # `redirect_endpoint` beside it. Both fields are informational —
        # readers never validate them — so the bumps document the shape rather
        # than gating anything.
        self.assertEqual(4, payload["version"])
        self.assertIsNone(payload["redirect_endpoint"])
        self.assertEqual(["/w/apple", "/w/zebra"], payload["repositories"])
        self.assertEqual(("/w/apple", "/w/zebra"), lock_info.repository_roots)

    def test_lock_without_a_claim_publishes_an_empty_registry(self) -> None:
        lock_info = state_locks.acquire_lock(self.plan_dir, self.state_dir)
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual([], self.read_payload()["repositories"])
        self.assertEqual((), lock_info.repository_roots)

    def test_version_one_lock_declares_no_repositories(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "owner": "agent-session-loop",
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "plan_dir": str(plans_files.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T12:00:00+00:00",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        lock_info = state_locks.read_lock_info(self.plan_dir, self.state_dir)

        assert lock_info is not None
        self.assertTrue(lock_info.active)
        self.assertEqual((), lock_info.repository_roots)

    def test_a_lock_nested_past_the_decoders_depth_reads_as_invalid(self) -> None:
        # json.loads raises RecursionError, not ValueError, on deep nesting;
        # every lock reader must classify the file rather than crash on it.
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text("[" * 200_000, encoding="utf-8")

        lock_info = state_locks.read_lock_info(self.plan_dir, self.state_dir)

        assert lock_info is not None
        self.assertTrue(lock_info.stale)
        self.assertIsNone(lock_info.pid)
        self.assertTrue(lock_info.reason.startswith("invalid lock file:"))

    def test_a_malformed_repositories_value_reads_as_no_claim(self) -> None:
        raw = {"version": 2, "repositories": {"a": "/w/apple"}}
        lock_info = state.LockInfo(
            path=self.lock_path,
            pid=os.getpid(),
            plan_dir=str(self.plan_dir),
            created_at="2026-06-19T12:00:00+00:00",
            stale=False,
            reason="active",
            raw=raw,
        )

        self.assertEqual((), lock_info.repository_roots)

    def test_a_reader_never_observes_a_partial_lock(self) -> None:
        observed: list[state.LockInfo | None] = []
        real_replace = os.replace

        def observing_replace(src: str | Path, dst: str | Path) -> None:
            # Whatever a concurrent reader sees at the lock path, it sees it
            # here: everything before this call happened on the sibling.
            observed.append(state_locks.read_lock_info(self.plan_dir, self.state_dir))
            real_replace(src, dst)

        with mock.patch.object(state.os, "replace", observing_replace):
            state_locks.acquire_lock(
                self.plan_dir, self.state_dir, repository_roots=("/w/apple",)
            )
        self.addCleanup(state_locks.release_lock, self.plan_dir, self.state_dir)

        self.assertEqual([None], observed)
        self.assertEqual(["/w/apple"], self.read_payload()["repositories"])
        self.assertEqual(
            [state.LOCK_FILE_NAME, state_locks.LOCK_MUTEX_FILE_NAME],
            sorted(path.name for path in self.lock_path.parent.iterdir()),
        )

    def test_a_failed_payload_write_leaves_no_lock_at_the_lock_path(self) -> None:
        with (
            mock.patch.object(
                durability.os, "replace", side_effect=RuntimeError("disk gave up")
            ),
            self.assertRaises(RuntimeError),
        ):
            state_locks.acquire_lock(self.plan_dir, self.state_dir)

        self.assertFalse(self.lock_path.exists())
        self.assertEqual(
            [state_locks.LOCK_MUTEX_FILE_NAME],
            sorted(path.name for path in self.lock_path.parent.iterdir()),
        )


def _plan_state_dir(state_root: Path, plan_dir: Path) -> Path:
    entry = state_root / plans_files.path_sha256(plan_dir)
    entry.mkdir(parents=True, exist_ok=True)
    return entry


def _write_state_json_entry(state_root: Path, plan_dir: Path) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    (entry / events.STATE_FILE_NAME).write_text(
        json.dumps({"plan_dir": str(plan_dir)}, sort_keys=True),
        encoding="utf-8",
    )
    return entry


def _write_lock_json_entry(state_root: Path, plan_dir: Path) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    (entry / state.LOCK_FILE_NAME).write_text(
        json.dumps(
            {"plan_dir": str(plan_dir), "pid": 1, "created_at": "x"},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return entry


def _write_result_json_entry(
    state_root: Path,
    plan_dir: Path,
    *,
    section: str = "after",
    run_id: str = "run-1",
    turn_label: str = "1",
) -> Path:
    entry = _plan_state_dir(state_root, plan_dir)
    turn_dir = (
        entry / state.RUNS_DIR_NAME / run_id / f"{state.TURN_DIR_PREFIX}{turn_label}"
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    (turn_dir / state.RESULT_FILE_NAME).write_text(
        json.dumps({section: {"plan_dir": str(plan_dir)}}, sort_keys=True),
        encoding="utf-8",
    )
    return entry


class ResolvePlanReferenceTests(unittest.TestCase):
    """Focused tests for the read-only state-corpus plan-label resolver."""

    def test_existing_direct_directory_wins_without_state_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "compact-label"
            plan_dir.mkdir()
            # A non-directory state root would raise if scanned; the existing
            # directory must short-circuit before any lookup happens.
            not_a_dir = root / "state-file"
            not_a_dir.write_text("not a dir", encoding="utf-8")

            resolved = state_plan_refs.resolve_plan_reference(str(plan_dir), not_a_dir)

            self.assertEqual(plans_files.canonicalize_path(plan_dir), resolved)

    def test_unique_match_from_state_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "abc123")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("abc123", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_unique_match_from_lock_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "lock-label")
            plan_dir.mkdir(parents=True)
            _write_lock_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("lock-label", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_unique_match_from_result_only_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "res-label")
            plan_dir.mkdir(parents=True)
            _write_result_json_entry(state_root, plan_dir, section="after")

            resolved = state_plan_refs.resolve_plan_reference("res-label", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_result_before_plan_dir_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "before-label")
            plan_dir.mkdir(parents=True)
            _write_result_json_entry(state_root, plan_dir, section="before")

            resolved = state_plan_refs.resolve_plan_reference(
                "before-label", state_root
            )

            self.assertEqual(plan_dir, resolved)

    def test_candidate_deduplication_across_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "dedup")
            plan_dir.mkdir(parents=True)
            # Same plan referenced by all three sources in one state dir.
            _write_state_json_entry(state_root, plan_dir)
            _write_lock_json_entry(state_root, plan_dir)
            _write_result_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("dedup", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_case_sensitive_basename_matching_misses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "Alpha")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("alpha", state_root)

            # No case-insensitive match: falls back to the canonical argument.
            self.assertEqual(plans_files.canonicalize_path("alpha"), resolved)

    def test_path_like_nonexistent_argument_bypasses_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "bar")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("foo/bar", state_root)

            # Directory component present: keep path-only behavior.
            self.assertEqual(plans_files.canonicalize_path("foo/bar"), resolved)

    def test_missing_state_root_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "absent"

            resolved = state_plan_refs.resolve_plan_reference("whatever", state_root)

            self.assertEqual(plans_files.canonicalize_path("whatever"), resolved)

    def test_unknown_label_retains_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "known")
            plan_dir.mkdir(parents=True)
            _write_state_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("missing", state_root)

            self.assertEqual(plans_files.canonicalize_path("missing"), resolved)

    def test_nonexistent_candidate_plan_dir_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = root / "plans" / "gone"
            # Register metadata but never create the plan directory itself.
            _write_state_json_entry(state_root, plans_files.canonicalize_path(plan_dir))

            resolved = state_plan_refs.resolve_plan_reference("gone", state_root)

            self.assertEqual(plans_files.canonicalize_path("gone"), resolved)

    def test_ambiguous_matches_raise_sorted_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            first = plans_files.canonicalize_path(root / "b-parent" / "dup")
            second = plans_files.canonicalize_path(root / "a-parent" / "dup")
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            _write_state_json_entry(state_root, first)
            _write_lock_json_entry(state_root, second)

            with self.assertRaises(state.AmbiguousPlanError) as caught:
                state_plan_refs.resolve_plan_reference("dup", state_root)

            error = caught.exception
            self.assertIsInstance(error, state.StateError)
            self.assertEqual("dup", error.label)
            self.assertEqual(state_root, error.state_root)
            self.assertEqual(sorted([first, second], key=str), error.candidates)

    def test_malformed_individual_record_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_root = root / "state"
            plan_dir = plans_files.canonicalize_path(root / "plans" / "good")
            plan_dir.mkdir(parents=True)
            # A malformed state.json in the same entry must not block the
            # valid result.json fallback.
            entry = _plan_state_dir(state_root, plan_dir)
            (entry / events.STATE_FILE_NAME).write_text("{ not json", encoding="utf-8")
            _write_result_json_entry(state_root, plan_dir)

            resolved = state_plan_refs.resolve_plan_reference("good", state_root)

            self.assertEqual(plan_dir, resolved)

    def test_non_directory_state_root_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "root-file"
            state_root.write_text("not a directory", encoding="utf-8")

            with self.assertRaises(state.StateError):
                state_plan_refs.resolve_plan_reference("label", state_root)

    def test_unenumerable_state_root_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            state_root.mkdir()

            with (
                mock.patch.object(Path, "iterdir", side_effect=OSError("boom")),
                self.assertRaises(state.StateError),
            ):
                state_plan_refs.resolve_plan_reference("label", state_root)
