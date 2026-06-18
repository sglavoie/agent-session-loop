from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_session_loop import (
    cli,
    config,
    events,
    harnesses,
    plans,
    run_control,
    runner,
    state,
    tail,
)
from agent_session_loop.formatting import format_duration_seconds


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CODEX_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_codex.py"
FAKE_SPEC_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "fake_spec.py"


class TTYText(io.StringIO):
    def __init__(self, value: str = "", *, tty: bool = True, fd: int = 10) -> None:
        super().__init__(value)
        self._tty = tty
        self._fd = fd

    def isatty(self) -> bool:
        return self._tty

    def fileno(self) -> int:
        return self._fd


@contextmanager
def patched_env(**updates: str | None):
    originals = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in originals.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def write_plan(plan_dir: Path, statuses: tuple[str, ...]) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    for number, status_value in enumerate(statuses, start=1):
        (plan_dir / f"session-{number}.md").write_text(
            "\n".join(
                [
                    f"# Session {number}: Test",
                    "",
                    "Instructions.",
                    "",
                    "## Handoff",
                    "",
                    f"- **Status**: {status_value}",
                    "- **Files modified**: (none)",
                    "- **Key decisions**: (none)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def write_plan_with_ids(plan_dir: Path, statuses: tuple[tuple[str, str], ...]) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    for session_id, status_value in statuses:
        display_label = plans.session_id_display_label(session_id)
        (plan_dir / plans.session_id_to_filename(session_id)).write_text(
            "\n".join(
                [
                    f"# {display_label}: Test",
                    "",
                    "Instructions.",
                    "",
                    "## Handoff",
                    "",
                    f"- **Status**: {status_value}",
                    "- **Files modified**: (none)",
                    "- **Key decisions**: (none)",
                    "- **Blockers for next session**: (none)",
                    "- **Notes**:",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def install_fake_codex(tmp_dir: Path) -> Path:
    fake_path = tmp_dir / "codex"
    shutil.copyfile(FAKE_CODEX_FIXTURE, fake_path)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def install_fake_spec(tmp_dir: Path) -> Path:
    fake_path = tmp_dir / "spec"
    shutil.copyfile(FAKE_SPEC_FIXTURE, fake_path)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def init_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "agent-session-loop@example.com"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agent Session Loop Tests"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    (repo_dir / "README.md").write_text("test repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )


def git_stdout(repo_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def fail_open_for_path(target: Path):
    canonical_target = plans.canonicalize_path(target)
    original_open = Path.open

    def failing_open(path: Path, *args, **kwargs):
        if plans.canonicalize_path(path) == canonical_target:
            raise PermissionError(
                errno.EACCES, "permission denied", os.fspath(canonical_target)
            )
        return original_open(path, *args, **kwargs)

    return mock.patch.object(Path, "open", failing_open)


def wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


class EventPersistenceTests(unittest.TestCase):
    def test_append_event_writes_complete_json_lines_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME

            def append_many(worker: int) -> None:
                for index in range(50):
                    events.append_event(
                        event_path,
                        events.Event(
                            name="threaded",
                            payload={"worker": worker, "index": index},
                        ),
                    )

            threads = [
                threading.Thread(target=append_many, args=(worker,))
                for worker in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

            lines = event_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(400, len(lines))
            records = [json.loads(line) for line in lines]
            observed = {
                (record["payload"]["worker"], record["payload"]["index"])
                for record in records
            }
            expected = {
                (worker, index)
                for worker in range(8)
                for index in range(50)
            }
            self.assertEqual(expected, observed)


class ParserAndStateTests(unittest.TestCase):
    def test_format_duration_seconds_uses_seconds_minutes_and_hours(self) -> None:
        cases = [
            (None, "unknown"),
            ("not-a-number", "unknown"),
            (float("nan"), "unknown"),
            (0, "0.0s"),
            (12.34, "12.3s"),
            (59.94, "59.9s"),
            (59.95, "1m00s"),
            (60, "1m00s"),
            (65.4, "1m05s"),
            (65.5, "1m06s"),
            (5405, "1h30m05s"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, format_duration_seconds(value))

    def test_tail_event_messages_format_long_durations(self) -> None:
        heartbeat = tail._event_message(
            events.TURN_HEARTBEAT_EVENT,
            {
                "session": 2,
                "turn_index": 3,
                "elapsed_seconds": 5405,
                "idle_seconds": 65,
                "last_activity_at": None,
                "last_activity_stream": "stderr",
            },
        )
        self.assertIn("elapsed 1h30m05s", heartbeat)
        self.assertIn("idle 1m05s", heartbeat)

        finished = tail._event_message(
            "turn_finished",
            {
                "session": 2,
                "turn_index": 3,
                "status_result": "complete",
                "returncode": 0,
                "duration_seconds": 90,
            },
        )
        self.assertIn("duration 1m30s", finished)

    def test_tail_event_messages_format_run_controls(self) -> None:
        requested = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": "stop-after-current-requested",
                "message": "stop-after-current requested",
                "stop_after_current": True,
                "session": 2,
                "turn_index": 3,
                "total_sessions": 5,
            },
        )
        self.assertEqual(
            "run control: stop-after-current requested "
            "(stop-after-current on, session 2/5, turn 3)",
            requested,
        )

        canceled = tail._event_message(
            events.RUN_CONTROL_EVENT,
            {
                "action": "stop-after-current-canceled",
                "message": "stop-after-current canceled",
                "stop_after_current": False,
            },
        )
        self.assertEqual(
            "run control: stop-after-current canceled (stop-after-current off)",
            canceled,
        )

    def test_session_id_helpers_parse_sort_and_reject_invalid_ids(self) -> None:
        parsed = [
            plans.parse_session_id(value)
            for value in ("2.10", "2", "10", "2.1", "2.2")
        ]

        self.assertEqual(
            ["2", "2.1", "2.2", "2.10", "10"],
            [
                session_id.value
                for session_id in sorted(parsed, key=lambda item: item.sort_key)
            ],
        )
        self.assertEqual((2, 1), plans.parse_session_id("2.1").components)
        self.assertEqual("session-2.1.md", plans.session_id_to_filename("2.1"))
        self.assertEqual(
            "session-2.1.expand.md",
            plans.session_id_to_expansion_filename("2.1"),
        )
        self.assertEqual("Session 2.1", plans.session_id_display_label("2.1"))

        for value in ("0", "01", "1.0", "1.", "1..2", "1.a", "a"):
            with self.subTest(value=value):
                self.assertFalse(plans.is_valid_session_id(value))
                with self.assertRaises(ValueError):
                    plans.parse_session_id(value)

    def test_session_filename_helpers_match_dotted_files_exactly(self) -> None:
        session_id = plans.session_id_from_filename("session-2.1.md")
        expansion_id = plans.session_id_from_expansion_filename(
            "session-2.1.expand.md"
        )

        self.assertIsNotNone(session_id)
        self.assertIsNotNone(expansion_id)
        self.assertEqual("2.1", session_id.value)
        self.assertEqual("2.1", expansion_id.value)

        self.assertIsNone(plans.session_id_from_filename("session-2.1.expand.md"))
        self.assertIsNone(plans.session_id_from_filename("session-02.md"))
        self.assertIsNone(
            plans.session_id_from_expansion_filename("session-2.1.md")
        )
        self.assertIsNone(
            plans.session_id_from_expansion_filename("session-2.01.expand.md")
        )

    def test_inspect_plan_reports_noncanonical_leading_zero_session_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (plan_dir / "session-01.md").write_text(
                "# Session 01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )
            (plan_dir / "session-2.01.md").write_text(
                "# Session 2.01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )
            (plan_dir / "session-01.expand.md").write_text(
                "ignored for session discovery\n", encoding="utf-8"
            )

            with self.assertRaises(plans.PlanError) as raised:
                plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn("no canonical session-<id>.md files found", message)
            self.assertIn("leading zeroes are invalid", message)
            self.assertIn("session-01.md -> session-1.md", message)
            self.assertIn("session-2.01.md -> session-2.1.md", message)

    def test_inspect_plan_sorts_exact_session_files_and_finds_next_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending", "complete"))
            (plan_dir / "session-10.expand.md").write_text(
                "ignored\n", encoding="utf-8"
            )
            (plan_dir / "session-10.md").write_text(
                "# Session 10\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual(
                [1, 2, 3, 10], [session.number for session in snapshot.sessions]
            )
            self.assertEqual(
                ["complete", "pending", "complete", "pending"],
                [session.status for session in snapshot.sessions],
            )
            self.assertEqual(2, snapshot.next_pending.number)
            self.assertEqual(plans.path_sha256(plan_dir), snapshot.path_hash)

    def test_inspect_plan_sorts_dotted_session_files_and_finds_nested_next_pending(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (
                    ("1", "complete"),
                    ("2.10", "pending"),
                    ("2.1", "complete"),
                    ("2", "complete"),
                    ("10", "pending"),
                    ("2.2", "pending"),
                ),
            )
            (plan_dir / "session-2.1.expand.md").write_text(
                "ignored for discovery\n", encoding="utf-8"
            )
            (plan_dir / "session-2.01.md").write_text(
                "invalid id ignored\n", encoding="utf-8"
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual(
                ["1", "2", "2.1", "2.2", "2.10", "10"],
                [session.id for session in snapshot.sessions],
            )
            self.assertEqual("2.2", snapshot.next_pending.id)
            self.assertEqual(
                ["complete", "complete", "complete", "pending", "pending", "pending"],
                [session.status for session in snapshot.sessions],
            )

    def test_inspect_plan_reads_status_from_final_handoff_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1",
                        "",
                        "Instructions.",
                        "",
                        "**Handoff (fill on completion)**:",
                        "",
                        "- **Status**:",
                        "- **Files modified**:",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: done",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)

            self.assertEqual("complete", snapshot.sessions[0].status)
            self.assertEqual("complete", snapshot.sessions[0].raw_value)
            self.assertEqual(12, snapshot.sessions[0].line_number)

    def test_plan_content_hash_tracks_matching_dotted_expansion_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))
            ignored = plan_dir / "session-1.1.expand.md.bak"
            ignored.write_text("ignored\n", encoding="utf-8")

            without_expansion = plans.plan_content_sha256(plan_dir)
            expansion = plan_dir / "session-1.1.expand.md"
            expansion.write_text("first\n", encoding="utf-8")
            with_expansion = plans.plan_content_sha256(plan_dir)
            expansion.write_text("second\n", encoding="utf-8")
            changed_expansion = plans.plan_content_sha256(plan_dir)

            self.assertNotEqual(without_expansion, with_expansion)
            self.assertNotEqual(with_expansion, changed_expansion)
            ignored_hash = plans.plan_content_sha256(plan_dir)
            ignored.write_text("still ignored\n", encoding="utf-8")
            self.assertEqual(ignored_hash, plans.plan_content_sha256(plan_dir))

    def test_sync_derived_status_ledgers_updates_clear_status_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete", "pending"))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| # | Title | Status | File |",
                        "|---|---|---|---|",
                        "| 1 | One | pending | [session-1.md](session-1.md) |",
                        "| 2 | Two | complete | session-2.md |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| S1 | One | pending |",
                        "| S2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1 | One | complete | [session-1.md](session-1.md) |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1 | One | complete |",
                context_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S2 | Two | pending |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_updates_dotted_session_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(
                plan_dir,
                (("1", "complete"), ("1.1", "pending"), ("2", "pending")),
            )
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session | Title | Status | File |",
                        "|---|---|---|---|",
                        "| S1 | One | pending | session-1.md |",
                        "| S1.1 | Nested | complete | session-1.1.md |",
                        "| 2 | Two | complete | session-2.md |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| Session | Title | Status |",
                        "|---|---|---|",
                        "| `S1` | One | pending |",
                        "| `S1.1` | Nested | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| S1 | One | complete | session-1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| S1.1 | Nested | pending | session-1.1.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 2 | Two | pending | session-2.md |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| `S1.1` | Nested | pending |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_accepts_id_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan_with_ids(plan_dir, (("1.1", "complete"),))
            manifest_path = plan_dir / "manifest.md"
            context_path = plan_dir / "context.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Manifest",
                        "",
                        "| Session ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            context_path.write_text(
                "\n".join(
                    [
                        "# Context",
                        "",
                        "| ID | Status |",
                        "|---|---|",
                        "| 1.1 | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual(
                {
                    plans.canonicalize_path(manifest_path),
                    plans.canonicalize_path(context_path),
                },
                set(changed),
            )
            self.assertIn(
                "| 1.1 | complete |",
                manifest_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "| 1.1 | complete |",
                context_path.read_text(encoding="utf-8"),
            )

    def test_sync_derived_status_ledgers_leaves_ambiguous_context_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("complete",))
            context_path = plan_dir / "context.md"
            original_context = "\n".join(
                [
                    "# Context",
                    "",
                    "| Item | Status |",
                    "|---|---|",
                    "| Draft -> review | pending |",
                    "",
                ]
            )
            context_path.write_text(original_context, encoding="utf-8")

            snapshot = plans.inspect_plan(plan_dir)
            changed = plans.sync_derived_status_ledgers(snapshot)

            self.assertEqual((), changed)
            self.assertEqual(original_context, context_path.read_text(encoding="utf-8"))

    def test_unknown_and_empty_statuses_are_reported_as_invalid_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Status**: maybe\n",
                encoding="utf-8",
            )
            unknown = plans.inspect_plan(plan_dir)
            self.assertEqual("unknown", unknown.sessions[0].status)
            self.assertEqual("unknown-status", runner._invalid_status_pause(unknown)[0])

            (plan_dir / "session-1.md").write_text(
                "# Session 1\n\n## Handoff\n\n- **Files modified**: none\n",
                encoding="utf-8",
            )
            empty = plans.inspect_plan(plan_dir)
            self.assertEqual("empty", empty.sessions[0].status)
            self.assertEqual("malformed-plan", runner._invalid_status_pause(empty)[0])

    def test_inspect_plan_wraps_plan_file_read_failures(self) -> None:
        cases = (
            "context.md",
            "manifest.md",
            "parallel-validation.md",
            "session-1.expand.md",
        )

        for relative_name in cases:
            with self.subTest(relative_name=relative_name):
                with tempfile.TemporaryDirectory() as tmp:
                    plan_dir = Path(tmp) / "plan"
                    write_plan(plan_dir, ("pending",))
                    target = plan_dir / relative_name
                    if not target.exists():
                        target.write_text("extra plan metadata\n", encoding="utf-8")

                    with fail_open_for_path(target):
                        with self.assertRaises(plans.PlanError) as raised:
                            plans.inspect_plan(plan_dir)

                    message = str(raised.exception)
                    self.assertIn(str(plans.canonicalize_path(target)), message)
                    self.assertIn("permission denied", message.lower())
                    self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_inspect_plan_wraps_session_discovery_read_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            canonical_plan_dir = plans.canonicalize_path(plan_dir)
            original_iterdir = Path.iterdir

            def failing_iterdir(path: Path):
                if plans.canonicalize_path(path) == canonical_plan_dir:
                    raise PermissionError(
                        errno.EACCES, "permission denied", os.fspath(canonical_plan_dir)
                    )
                return original_iterdir(path)

            with mock.patch.object(Path, "iterdir", failing_iterdir):
                with self.assertRaises(plans.PlanError) as raised:
                    plans.inspect_plan(plan_dir)

            message = str(raised.exception)
            self.assertIn(str(canonical_plan_dir), message)
            self.assertIn("permission denied", message.lower())
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

    def test_acquire_lock_rejects_active_lock_and_replaces_stale_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            lock_info = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(lock_info.active)
            with self.assertRaises(state.StateError):
                state.acquire_lock(plan_dir, state_dir)
            state.release_lock(plan_dir, state_dir)

            lock_path = state.lock_path_for_plan(plan_dir, state_dir)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path.write_text(
                json.dumps(
                    {"pid": 0, "plan_dir": str(plans.canonicalize_path(plan_dir))}
                ),
                encoding="utf-8",
            )
            replaced = state.acquire_lock(plan_dir, state_dir)
            self.assertTrue(replaced.active)
            state.release_lock(plan_dir, state_dir)

    def test_status_payload_includes_json_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)

            self.assertTrue(payload["state_path"].endswith("/state.json"))
            self.assertTrue(payload["events_path"].endswith("/events.jsonl"))
            self.assertEqual(1, payload["counts"]["pending"])


class TailFollowerTests(unittest.TestCase):
    def setup_tail_files(self, tmp_dir: Path):
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        write_plan(plan_dir, ("pending",))
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True)
        return (
            plan_dir,
            state_dir,
            plan_state / events.STATE_FILE_NAME,
            plan_state / events.EVENTS_FILE_NAME,
        )

    def write_state_payload(
        self,
        state_path: Path,
        payload: dict[str, object],
    ) -> None:
        state_path.write_text(json.dumps(payload), encoding="utf-8")

    def append_log_line(self, path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)

    def cleanup_follower(self, follower: tail.TailFollower) -> None:
        follower.stop()
        follower.join(1.0)

    def test_tail_follower_starts_at_eof_for_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stderr_log = tmp_dir / "stderr.log"
            stdout_log.write_text("old stdout\n", encoding="utf-8")
            stderr_log.write_text("old stderr\n", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
                "stderr_path": str(stderr_log),
            }
            self.write_state_payload(
                state_path,
                {
                    "run_id": "run-1",
                    "active_turn": turn_payload,
                    "turns": [turn_payload],
                    "run_finished_at": None,
                },
            )
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload=turn_payload,
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "new stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_finished",
                    payload={
                        **turn_payload,
                        "status_result": "complete",
                        "returncode": 0,
                        "duration_seconds": 1.2,
                    },
                    created_at="2026-06-11T12:00:01+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 finished" in out.getvalue()
                    and "new stdout" in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertNotIn("old stdout", text)
            self.assertNotIn("old stderr", text)
            self.assertNotIn("session 1/1 started", text)
            self.assertIn("stdout session=1 turn=1 | new stdout", text)
            self.assertIn("session 1/1 finished", text)

    def test_tail_follower_discovers_log_targets_from_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            stdout_log.write_text("discovered stdout\n", encoding="utf-8")
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={
                        "run_id": "run-1",
                        "session": 1,
                        "session_id": "1",
                        "total_sessions": 1,
                        "turn_index": 1,
                        "stdout_path": str(stdout_log),
                    },
                    created_at="2026-06-11T12:00:00+00:00",
                ),
            )

            wait_until(
                lambda: (
                    "session 1/1 started" in out.getvalue()
                    and "stdout session=1 turn=1 | discovered stdout"
                    in out.getvalue()
                )
            )
            follower.stop()
            self.assertTrue(follower.join(1.0))

    def test_tail_follower_stop_cancels_following_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": None, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.assertFalse(follower.join(0.02))
            follower.stop()
            self.assertTrue(follower.join(1.0))
            self.assertTrue(follower.done)
            before = out.getvalue()
            events.append_event(
                events_path,
                events.Event(
                    name="turn_started",
                    payload={"run_id": "run-1", "session": 1, "turn_index": 1},
                ),
            )
            time.sleep(0.05)
            self.assertEqual(before, out.getvalue())

    def test_tail_follower_stops_after_run_finished_and_drains_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir, state_dir, state_path, events_path = self.setup_tail_files(
                tmp_dir
            )
            stdout_log = tmp_dir / "stdout.log"
            stdout_log.write_text("", encoding="utf-8")
            turn_payload = {
                "run_id": "run-1",
                "session": 1,
                "session_id": "1",
                "total_sessions": 1,
                "turn_index": 1,
                "stdout_path": str(stdout_log),
            }
            self.write_state_payload(
                state_path,
                {"run_id": "run-1", "active_turn": turn_payload, "turns": []},
            )
            out = io.StringIO()
            follower = tail.TailFollower(
                plan_dir,
                state_dir=state_dir,
                out=out,
                poll_interval=0.01,
            )
            self.addCleanup(self.cleanup_follower, follower)

            follower.start()
            self.append_log_line(stdout_log, "final stdout\n")
            events.append_event(
                events_path,
                events.Event(
                    name="run_finished",
                    payload={
                        "run_id": "run-1",
                        "ok": True,
                        "reason": "all-complete",
                        "duration_seconds": 1.0,
                    },
                    created_at="2026-06-11T12:00:02+00:00",
                ),
            )

            wait_until(lambda: follower.done)
            self.assertTrue(follower.join(1.0))
            text = out.getvalue()
            self.assertIn("run finished: run run-1", text)
            self.assertIn("stdout session=1 turn=1 | final stdout", text)


class CommandAndConfigTests(unittest.TestCase):
    def test_codex_command_uses_workspace_write_or_unsafe_bypass(self) -> None:
        adapter = harnesses.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        last_message = Path("/tmp/state/last-message-1.txt")

        safe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            last_message_path=last_message,
        )
        self.assertIn("-s", safe)
        self.assertIn("workspace-write", safe)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", safe)
        self.assertNotIn("-a", safe)

        unsafe = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="$session-run /tmp/plan/",
            unsafe_bypass=True,
            last_message_path=last_message,
        )
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", unsafe)
        self.assertNotIn("workspace-write", unsafe)

    def test_codex_command_includes_model_and_reasoning_effort_config(self) -> None:
        adapter = harnesses.CodexAdapter(executable="codex")
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")
        prompt = "$session-run /tmp/plan/"
        last_message = Path("/tmp/state/last-message-1.txt")

        argv = adapter.build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt=prompt,
            last_message_path=last_message,
            model="gpt-5",
            effort="high",
        )

        output_index = argv.index("-o")
        self.assertEqual("gpt-5", argv[argv.index("--model") + 1])
        self.assertEqual('model_reasoning_effort="high"', argv[argv.index("-c") + 1])
        self.assertLess(argv.index("--model"), output_index)
        self.assertLess(argv.index("-c"), output_index)
        self.assertEqual(str(last_message), argv[output_index + 1])
        self.assertEqual(prompt, argv[-1])

    def test_adapter_command_shapes_for_claude_and_copilot(self) -> None:
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        claude = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertEqual("dontAsk", claude[claude.index("--permission-mode") + 1])
        self.assertIn("--add-dir", claude)

        claude_unsafe = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            unsafe_bypass=True,
        )
        self.assertEqual(
            "bypassPermissions",
            claude_unsafe[claude_unsafe.index("--permission-mode") + 1],
        )

        copilot = harnesses.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
        )
        self.assertIn("--allow-all-tools", copilot)
        self.assertIn("--allow-all-paths", copilot)
        self.assertEqual(str(cwd), copilot[copilot.index("-C") + 1])

    def test_adapter_model_and_effort_flags_for_claude_and_copilot(self) -> None:
        cwd = Path("/tmp/repo")
        plan_dir = Path("/tmp/plan")

        claude = harnesses.ClaudeAdapter(executable="claude").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            model="claude-opus-4",
            effort="medium",
        )
        self.assertEqual("claude-opus-4", claude[claude.index("--model") + 1])
        self.assertEqual("medium", claude[claude.index("--effort") + 1])
        self.assertLess(claude.index("--model"), len(claude) - 1)
        self.assertLess(claude.index("--effort"), len(claude) - 1)
        self.assertEqual("/session-run /tmp/plan/", claude[-1])

        copilot = harnesses.CopilotAdapter(executable="copilot").build_argv(
            cwd=cwd,
            plan_dir=plan_dir,
            prompt="/session-run /tmp/plan/",
            model="gpt-5",
            effort="low",
        )
        prompt_index = copilot.index("-p")
        self.assertEqual("gpt-5", copilot[copilot.index("--model") + 1])
        self.assertEqual("low", copilot[copilot.index("--effort") + 1])
        self.assertLess(copilot.index("--model"), prompt_index)
        self.assertLess(copilot.index("--effort"), prompt_index)
        self.assertEqual("/session-run /tmp/plan/", copilot[prompt_index + 1])

    def test_custom_harness_config_accepts_supported_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "harnesses.json"
            cases = [
                ({"codex": {"executable": "/bin/codex"}}, "/bin/codex"),
                ({"codex": "/bin/codex-short"}, "/bin/codex-short"),
                (
                    {"harnesses": {"codex": {"executable": "/bin/codex-nested"}}},
                    "/bin/codex-nested",
                ),
            ]

            for raw, expected in cases:
                with self.subTest(raw=raw):
                    config_path.write_text(json.dumps(raw), encoding="utf-8")
                    self.assertEqual(
                        {"codex": {"executable": expected}},
                        config.load_harness_config(config_path),
                    )
                    self.assertEqual(
                        expected,
                        config.configured_harness_executable("codex", config_path),
                    )

    def test_native_model_payload_reads_codex_toml_and_copilot_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_config = home / ".codex" / "config.toml"
            copilot_settings = home / ".copilot" / "settings.json"
            codex_config.parent.mkdir(parents=True)
            copilot_settings.parent.mkdir(parents=True)
            codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
            copilot_settings.write_text(
                json.dumps({"model": "gpt-5-copilot"}),
                encoding="utf-8",
            )

            with patched_env(HOME=str(home)):
                self.assertEqual(
                    {
                        "model": "gpt-5-codex",
                        "model_source": config.MODEL_SOURCE_CODEX_CONFIG,
                    },
                    config.native_model_payload("codex"),
                )
                self.assertEqual(
                    {
                        "model": "gpt-5-copilot",
                        "model_source": config.MODEL_SOURCE_COPILOT_SETTINGS,
                    },
                    config.native_model_payload("copilot"),
                )

    def test_native_model_payload_falls_back_when_missing_or_unreadable(self) -> None:
        expected = {
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
        }
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patched_env(HOME=str(home)):
                self.assertEqual(expected, config.native_model_payload("codex"))
                self.assertEqual(expected, config.native_model_payload("copilot"))
                self.assertEqual(expected, config.native_model_payload("claude"))

                with mock.patch.object(
                    Path, "read_text", side_effect=OSError("denied")
                ):
                    self.assertEqual(expected, config.native_model_payload("codex"))
                    self.assertEqual(expected, config.native_model_payload("copilot"))

    def test_build_session_prompt_uses_harness_skill_prefix(self) -> None:
        self.assertTrue(
            runner.build_session_prompt("codex", "/tmp/plan").startswith(
                "$session-run "
            )
        )
        self.assertTrue(
            runner.build_session_prompt("claude", "/tmp/plan").startswith(
                "/session-run "
            )
        )

    def test_commit_flags_are_mutually_exclusive(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(
                [
                    "run",
                    "/tmp/plan",
                    "--cwd",
                    "/tmp/repo",
                    "--commit-per-session",
                    "--commit-at-end",
                ]
            )

        self.assertEqual(2, raised.exception.code)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_run_and_doctor_default_cwd_to_current_directory(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertEqual(".", run_args.cwd)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertEqual(".", doctor_args.cwd)

    def test_run_and_doctor_default_to_unsafe_bypass_with_opt_out(self) -> None:
        parser = cli.build_parser()

        run_args = parser.parse_args(["run", "/tmp/plan"])
        self.assertTrue(run_args.unsafe_bypass)

        run_explicit_args = parser.parse_args(["run", "/tmp/plan", "--unsafe-bypass"])
        self.assertTrue(run_explicit_args.unsafe_bypass)

        run_safe_args = parser.parse_args(
            ["run", "/tmp/plan", "--no-unsafe-bypass"]
        )
        self.assertFalse(run_safe_args.unsafe_bypass)

        doctor_args = parser.parse_args(
            ["doctor", "--harness", "codex", "--plan-dir", "/tmp/plan"]
        )
        self.assertTrue(doctor_args.unsafe_bypass)

        doctor_safe_args = parser.parse_args(
            [
                "doctor",
                "--harness",
                "codex",
                "--plan-dir",
                "/tmp/plan",
                "--no-unsafe-bypass",
            ]
        )
        self.assertFalse(doctor_safe_args.unsafe_bypass)

    def test_parser_uses_custom_program_name_in_help(self) -> None:
        parser = cli.build_parser(prog="asl")
        stdout = io.StringIO()

        self.assertTrue(parser.format_help().startswith("usage: asl "))
        with redirect_stdout(stdout), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["run", "--help"])

        self.assertEqual(0, raised.exception.code)
        self.assertIn("usage: asl run", stdout.getvalue())

    def test_tail_interactive_flag_is_rejected(self) -> None:
        parser = cli.build_parser()
        stderr = io.StringIO()

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            parser.parse_args(["tail", "--interactive", "/tmp/plan"])

        self.assertEqual(2, raised.exception.code)
        self.assertIn("unrecognized arguments: --interactive", stderr.getvalue())


class RunControlTests(unittest.TestCase):
    def run_args(self, *, json_output: bool = False, dry_run: bool = False):
        return mock.Mock(json=json_output, dry_run=dry_run)

    def test_run_control_state_toggles_and_consumes_stop_after_current(self) -> None:
        state = run_control.RunControlState()

        self.assertFalse(state.stop_after_current)
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
            },
            state.snapshot(),
        )
        requested = state.toggle_stop_after_current()
        self.assertEqual("request", requested.action)
        self.assertTrue(requested.stop_after_current)
        self.assertTrue(state.stop_after_current)

        canceled = state.toggle_stop_after_current()
        self.assertEqual("cancel", canceled.action)
        self.assertFalse(canceled.stop_after_current)
        self.assertFalse(state.stop_after_current)
        self.assertIsNone(state.consume_stop_after_current())

        state.toggle_stop_after_current()
        consumed = state.consume_stop_after_current()
        self.assertIsNotNone(consumed)
        assert consumed is not None
        self.assertEqual("consumed", consumed.action)
        self.assertFalse(consumed.stop_after_current)
        self.assertFalse(state.stop_after_current)

    def test_run_control_state_tracks_tail_and_next_harness(self) -> None:
        state = run_control.RunControlState(current_harness="codex")

        status = state.request_status_snapshot()
        self.assertEqual("status", status.action)
        self.assertFalse(status.tail_enabled)
        self.assertIsNone(status.next_harness)

        enabled = state.toggle_tail()
        self.assertEqual("tail-enabled", enabled.action)
        self.assertTrue(enabled.tail_enabled)
        self.assertTrue(state.tail_enabled)

        disabled = state.toggle_tail()
        self.assertEqual("tail-disabled", disabled.action)
        self.assertFalse(disabled.tail_enabled)
        self.assertFalse(state.tail_enabled)

        self.assertIsNone(state.select_harness("codex"))
        selected = state.select_harness("claude")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("harness-selected", selected.action)
        self.assertEqual("claude", selected.next_harness)
        self.assertEqual("claude", state.next_harness)
        self.assertIsNone(state.select_harness("claude"))

    def test_run_control_help_and_toggle_lines_include_state(self) -> None:
        state = run_control.RunControlState()
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        listener.handle_key("h")
        listener.handle_key("p")
        listener.handle_key("h")

        lines = stderr.getvalue().splitlines()
        self.assertEqual(
            "[2026-06-11 12:00:00] controls: p toggle stop-after-current "
            "(currently off); s show status; t toggle live tail (currently off); "
            "1 codex, 2 claude, 3 copilot select next harness "
            "(currently unchanged); h show help",
            lines[0],
        )
        self.assertEqual(
            "[2026-06-11 12:00:00] control: stop-after-current requested; "
            "press p to cancel",
            lines[1],
        )
        self.assertEqual(
            "[2026-06-11 12:00:00] controls: p toggle stop-after-current "
            "(currently on); s show status; t toggle live tail (currently off); "
            "1 codex, 2 claude, 3 copilot select next harness "
            "(currently unchanged); h show help",
            lines[2],
        )
        self.assertEqual(["request"], [action.action for action in actions])

    def test_run_control_listener_handles_status_tail_and_harness_keys(self) -> None:
        state = run_control.RunControlState(current_harness="codex")
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        status = listener.handle_key("s")
        tail_enabled = listener.handle_key("t")
        same_harness = listener.handle_key("1")
        next_harness = listener.handle_key("2")
        repeated_harness = listener.handle_key("2")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual("status", status.action)
        self.assertIsNotNone(tail_enabled)
        assert tail_enabled is not None
        self.assertEqual("tail-enabled", tail_enabled.action)
        self.assertIsNone(same_harness)
        self.assertIsNotNone(next_harness)
        assert next_harness is not None
        self.assertEqual("harness-selected", next_harness.action)
        self.assertEqual("claude", next_harness.next_harness)
        self.assertIsNone(repeated_harness)
        self.assertEqual(
            ["tail-enabled", "harness-selected"],
            [action.action for action in actions],
        )

        self.assertEqual(
            [
                "[2026-06-11 12:00:00] control status: "
                "stop-after-current off; live tail off; next harness unchanged",
                "[2026-06-11 12:00:00] control: live tail enabled; "
                "press t to disable",
                "[2026-06-11 12:00:00] control: next harness already codex",
                "[2026-06-11 12:00:00] control: next harness selected: claude",
                "[2026-06-11 12:00:00] control: next harness already claude",
            ],
            stderr.getvalue().splitlines(),
        )

    def test_run_control_status_callback_does_not_record_action(self) -> None:
        state = run_control.RunControlState()
        stderr = TTYText()
        actions: list[run_control.RunControlAction] = []
        statuses: list[run_control.RunControlAction] = []
        listener = run_control.RunControlListener(
            enabled=False,
            state=state,
            stderr=stderr,
            on_action=actions.append,
            on_status=statuses.append,
            timestamp=lambda: "2026-06-11 12:00:00",
        )

        status = listener.handle_key("s")

        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual("status", status.action)
        self.assertEqual([status], statuses)
        self.assertEqual([], actions)
        self.assertEqual("", stderr.getvalue())

    def test_run_control_event_contract_includes_runtime_actions(self) -> None:
        self.assertIn("harness-unavailable", events.PAUSE_REASONS)
        self.assertIn("tail-enabled", events.RUN_CONTROL_ACTIONS)
        self.assertIn("tail-disabled", events.RUN_CONTROL_ACTIONS)
        self.assertIn("harness-selected", events.RUN_CONTROL_ACTIONS)

    def test_run_controls_require_text_tty_run(self) -> None:
        args = self.run_args()
        self.assertTrue(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                self.run_args(json_output=True),
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                self.run_args(dry_run=True),
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=False),
                stderr=TTYText(tty=True),
            )
        )
        self.assertFalse(
            run_control.should_enable_run_controls(
                args,
                stdin=TTYText(tty=True),
                stderr=TTYText(tty=False),
            )
        )

    def test_disabled_run_control_listener_never_touches_terminal(self) -> None:
        listener = run_control.RunControlListener.for_run(
            self.run_args(json_output=True),
            stdin=TTYText(tty=True),
            stderr=TTYText(tty=True),
        )

        with mock.patch.object(run_control.termios, "tcgetattr") as tcgetattr:
            self.assertFalse(listener.start())

        tcgetattr.assert_not_called()

    def test_run_control_start_stop_restores_terminal(self) -> None:
        original_settings = ["original"]
        thread = mock.Mock()
        thread.is_alive.return_value = False

        with (
            mock.patch.object(
                run_control.termios,
                "tcgetattr",
                return_value=original_settings,
            ) as tcgetattr,
            mock.patch.object(run_control.tty, "setcbreak") as setcbreak,
            mock.patch.object(run_control.termios, "tcsetattr") as tcsetattr,
            mock.patch.object(run_control.threading, "Thread", return_value=thread),
        ):
            listener = run_control.RunControlListener(
                enabled=True,
                stdin=TTYText(tty=True, fd=42),
                stderr=TTYText(tty=True),
            )
            self.assertTrue(listener.start())
            listener.stop()

        tcgetattr.assert_called_once_with(42)
        setcbreak.assert_called_once_with(42)
        thread.start.assert_called_once_with()
        tcsetattr.assert_called_once_with(
            42,
            run_control.termios.TCSADRAIN,
            original_settings,
        )

    def test_run_control_start_failure_restores_terminal(self) -> None:
        original_settings = ["original"]
        thread = mock.Mock()
        thread.start.side_effect = RuntimeError("thread failed")

        with (
            mock.patch.object(
                run_control.termios,
                "tcgetattr",
                return_value=original_settings,
            ),
            mock.patch.object(run_control.tty, "setcbreak"),
            mock.patch.object(run_control.termios, "tcsetattr") as tcsetattr,
            mock.patch.object(run_control.threading, "Thread", return_value=thread),
        ):
            listener = run_control.RunControlListener(
                enabled=True,
                stdin=TTYText(tty=True, fd=43),
                stderr=TTYText(tty=True),
            )
            self.assertFalse(listener.start())
            self.assertFalse(listener.running)

        tcsetattr.assert_called_once_with(
            43,
            run_control.termios.TCSADRAIN,
            original_settings,
        )


class FinishNotificationTests(unittest.TestCase):
    def finish_payload(self, tmp_dir: Path) -> dict[str, object]:
        return {
            "run_id": "run-123",
            "plan_dir": str(tmp_dir / "plan"),
            "cwd": str(tmp_dir / "repo"),
            "reason": "all-complete",
            "run_started_at": "2026-06-11T12:00:00Z",
            "run_finished_at": "2026-06-11T12:00:03Z",
            "state_path": str(tmp_dir / "state.json"),
        }

    def test_finish_notification_builds_agent_mux_command_and_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            completed = subprocess.CompletedProcess(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopFinished"],
                0,
                stdout="",
                stderr="",
            )

            with (
                mock.patch.object(
                    runner.shutil,
                    "which",
                    return_value="/opt/amx",
                ) as which,
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                runner._notify_agent_mux_loop_finished(payload)

            which.assert_called_once_with("amx")
            run.assert_called_once()
            args, kwargs = run.call_args
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopFinished"],
                args[0],
            )
            self.assertTrue(kwargs["text"])
            self.assertEqual(subprocess.PIPE, kwargs["stdout"])
            self.assertEqual(subprocess.PIPE, kwargs["stderr"])
            self.assertEqual(
                runner.AGENT_MUX_FINISH_NOTIFY_TIMEOUT_SECONDS,
                kwargs["timeout"],
            )
            self.assertFalse(kwargs["check"])
            self.assertEqual(
                {
                    "source": "agent_session_loop",
                    "reason": "run_finished",
                    "run_id": "run-123",
                    "plan_dir": payload["plan_dir"],
                    "cwd": payload["cwd"],
                    "run_finished_at": "2026-06-11T12:00:03Z",
                },
                json.loads(kwargs["input"]),
            )

    def test_finish_notification_skips_when_amx_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(runner.shutil, "which", return_value=None),
                mock.patch.object(runner.subprocess, "run") as run,
            ):
                runner._notify_agent_mux_loop_finished(self.finish_payload(Path(tmp)))

            run.assert_not_called()

    def test_finish_notification_swallows_subprocess_failures(self) -> None:
        failures: tuple[Exception, ...] = (
            subprocess.TimeoutExpired(["/opt/amx"], 1),
            OSError("amx failed"),
            subprocess.SubprocessError("amx failed"),
        )

        with tempfile.TemporaryDirectory() as tmp:
            payload = self.finish_payload(Path(tmp))
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with (
                        mock.patch.object(
                            runner.shutil,
                            "which",
                            return_value="/opt/amx",
                        ),
                        mock.patch.object(
                            runner.subprocess,
                            "run",
                            side_effect=failure,
                        ),
                    ):
                        runner._notify_agent_mux_loop_finished(payload)

    def test_finish_run_notifies_after_setting_finished_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / "events.jsonl"
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None

            with mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
            ) as notify:
                runner._finish_run(event_path, payload)

            notify.assert_called_once_with(payload)
            self.assertIsNotNone(payload["run_finished_at"])
            self.assertIsNone(payload["active_turn"])
            self.assertTrue(Path(payload["state_path"]).exists())
            records = read_jsonl(event_path)
            self.assertEqual("run_finished", records[0]["name"])
            self.assertEqual(
                payload["run_finished_at"],
                records[0]["payload"]["run_finished_at"],
            )


class FakeRunControlListener:
    def __init__(
        self,
        *,
        consume_on_call: int | None = None,
        actions_on_start: tuple[run_control.RunControlAction, ...] = (),
        status_on_start: tuple[run_control.RunControlAction, ...] = (),
        state_path: Path | None = None,
        events_path: Path | None = None,
    ) -> None:
        self.consume_on_call = consume_on_call
        self.actions_on_start = actions_on_start
        self.status_on_start = status_on_start
        self.state_path = state_path
        self.events_path = events_path
        self.on_action: run_control.ActionCallback | None = None
        self.on_status: run_control.StatusCallback | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self.consume_calls = 0
        self.state_exists_at_start = False
        self.events_exists_at_start = False

    def start(self) -> bool:
        self.start_calls += 1
        self.state_exists_at_start = (
            self.state_path.exists() if self.state_path is not None else False
        )
        self.events_exists_at_start = (
            self.events_path.exists() if self.events_path is not None else False
        )
        if self.on_action is not None:
            for action in self.actions_on_start:
                self.on_action(action)
        if self.on_status is not None:
            for action in self.status_on_start:
                self.on_status(action)
        return True

    def stop(self) -> None:
        self.stop_calls += 1

    def consume_stop_after_current(self) -> run_control.RunControlAction | None:
        self.consume_calls += 1
        if self.consume_on_call is None or self.consume_calls != self.consume_on_call:
            return None
        return run_control.RunControlAction(
            action="consumed",
            stop_after_current=False,
        )


class FakeHarnessIntegrationTests(unittest.TestCase):
    def setup_loop(
        self,
        *,
        statuses: tuple[str, ...],
        harness_config_overrides: dict[str, str] | None = None,
    ) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fake_codex = install_fake_codex(tmp_dir)
        harness_config = {"codex": {"executable": str(fake_codex)}}
        if harness_config_overrides is not None:
            for harness_name, executable in harness_config_overrides.items():
                harness_config[harness_name] = {"executable": executable}
        config_path.write_text(
            json.dumps(harness_config),
            encoding="utf-8",
        )
        write_plan(plan_dir, statuses)
        return plan_dir, state_dir, config_home, fake_codex

    def setup_loop_with_ids(
        self,
        *,
        statuses: tuple[tuple[str, str], ...],
    ) -> tuple[Path, Path, Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = Path(tmp.name)
        plan_dir = tmp_dir / "plan"
        state_dir = tmp_dir / "state"
        config_home = tmp_dir / "config"
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fake_codex = install_fake_codex(tmp_dir)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(fake_codex)}}),
            encoding="utf-8",
        )
        write_plan_with_ids(plan_dir, statuses)
        return plan_dir, state_dir, config_home, fake_codex

    def setup_fake_spec(
        self,
        tmp_dir: Path,
        *,
        mode: str = "success",
    ) -> tuple[Path, Path, dict[str, str]]:
        fake_spec = install_fake_spec(tmp_dir)
        log_path = tmp_dir / "fake-spec.jsonl"
        env = {
            "PATH": str(tmp_dir) + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SPEC_LOG": str(log_path),
        }
        if mode != "success":
            env["FAKE_SPEC_MODE"] = mode
        return fake_spec, log_path, env

    def event_index(
        self,
        records: list[dict[str, object]],
        name: str,
        **payload_values: object,
    ) -> int:
        for index, record in enumerate(records):
            if record.get("name") != name:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if all(payload.get(key) == value for key, value in payload_values.items()):
                return index
        raise AssertionError(f"event not found: {name} {payload_values}")

    def run_locked_with_listener(
        self,
        *,
        mode: str,
        statuses: tuple[str, ...],
        listener: FakeRunControlListener,
        cwd: Path | None = None,
        commit_mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
        harness_config_overrides: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=statuses,
            harness_config_overrides=harness_config_overrides,
        )
        real_state_dir = state.plan_state_dir(plan_dir, state_dir)
        if listener.state_path is None:
            listener.state_path = real_state_dir / events.STATE_FILE_NAME
        if listener.events_path is None:
            listener.events_path = real_state_dir / events.EVENTS_FILE_NAME
        run_cwd = REPO_ROOT if cwd is None else cwd
        args = argparse.Namespace(
            plan_dir=str(plan_dir),
            cwd=str(run_cwd),
            harness="codex",
            state_dir=str(state_dir),
            json=False,
            dry_run=False,
            unsafe_bypass=True,
            timeout=None,
            max_sessions=None,
            commit_mode=commit_mode,
            model=model,
            effort=effort,
        )
        env_updates = {
            "XDG_CONFIG_HOME": str(config_home),
            "FAKE_CODEX_MODE": mode,
            "FAKE_CODEX_SLEEP": "5",
            "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        }
        if extra_env is not None:
            env_updates.update(extra_env)

        def fake_for_run(*_args, **kwargs):
            listener.on_action = kwargs.get("on_action")
            listener.on_status = kwargs.get("on_status")
            return listener

        with (
            patched_env(**env_updates),
            mock.patch.object(
                runner.run_control.RunControlListener,
                "for_run",
                side_effect=fake_for_run,
            ),
        ):
            lock_info = state.acquire_lock(plan_dir, state_dir)
            try:
                exit_code, payload = runner._run_locked(
                    args=args,
                    cwd=run_cwd,
                    timeout=None,
                    max_sessions=None,
                    commit_mode=commit_mode,
                    lock_info=lock_info,
                    run_id="test-run",
                )
            finally:
                state.release_lock(plan_dir, state_dir)
        return exit_code, payload, plan_dir, state_dir, fake_codex

    def run_with_mocked_harness_switch(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object], list[dict[str, object]], Path, Path]:
        listener = FakeRunControlListener()
        calls: list[dict[str, object]] = []

        def complete_next_pending(plan_dir: Path) -> None:
            for session_file in sorted(plan_dir.glob("session-*.md")):
                text = session_file.read_text(encoding="utf-8")
                if "- **Status**: pending" not in text:
                    continue
                session_file.write_text(
                    text.replace("- **Status**: pending", "- **Status**: complete", 1),
                    encoding="utf-8",
                )
                return

        def fake_run_harness(name: str, **kwargs: object) -> harnesses.HarnessResult:
            calls.append({"harness": name, **kwargs})
            complete_next_pending(Path(kwargs["plan_dir"]))
            if len(calls) == 1 and listener.on_action is not None:
                listener.on_action(
                    run_control.RunControlAction(
                        action="harness-selected",
                        stop_after_current=False,
                        next_harness="claude",
                    )
                )
            return harnesses.HarnessResult(
                name=name,
                argv=[name, str(kwargs["prompt"])],
                returncode=0,
                stdout=f"{name} completed\n",
                stderr="",
            )

        with mock.patch.object(
            runner.harnesses,
            "run_harness",
            side_effect=fake_run_harness,
        ):
            exit_code, payload, plan_dir, state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending"),
                    listener=listener,
                    model=model,
                    effort=effort,
                    extra_env=extra_env,
                    harness_config_overrides={"claude": sys.executable},
                )
            )
        return exit_code, payload, calls, plan_dir, state_dir

    def run_loop_command(
        self,
        *,
        mode: str,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        cwd: Path | None = None,
        timeout: int | None = None,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        run_cwd = REPO_ROOT if cwd is None else cwd
        command = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(plan_dir),
            "--cwd",
            str(run_cwd),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
            "--json",
        ]
        if timeout is not None:
            command.extend(["--timeout", str(timeout)])
        if max_sessions is not None:
            command.extend(["--max-sessions", str(max_sessions)])
        if commit_mode == runner.COMMIT_MODE_PER_SESSION:
            command.append("--commit-per-session")
        elif commit_mode == runner.COMMIT_MODE_END:
            command.append("--commit-at-end")
        elif commit_mode is not None:
            raise ValueError(f"unsupported commit_mode: {commit_mode}")
        if model is not None:
            command.extend(["--model", model])
        if effort is not None:
            command.extend(["--effort", effort])

        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["FAKE_CODEX_MODE"] = mode
        env["FAKE_CODEX_SLEEP"] = "5"
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if extra_env is not None:
            env.update(extra_env)

        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def run_loop(
        self,
        *,
        mode: str,
        statuses: tuple[str, ...],
        cwd: Path | None = None,
        timeout: int | None = None,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object], Path, Path, Path]:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=statuses
        )
        completed, payload = self.run_loop_command(
            mode=mode,
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            cwd=cwd,
            timeout=timeout,
            max_sessions=max_sessions,
            commit_mode=commit_mode,
            model=model,
            effort=effort,
            extra_env=extra_env,
        )
        return completed, payload, plan_dir, state_dir, fake_codex

    def loop_env(
        self,
        *,
        config_home: Path,
        mode: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        env["FAKE_CODEX_MODE"] = mode
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        if extra is not None:
            env.update(extra)
        return env

    def run_command(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        cwd: Path | None = None,
        json_output: bool = False,
        max_sessions: int | None = None,
        commit_mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        run_cwd = REPO_ROOT if cwd is None else cwd
        command = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(plan_dir),
            "--cwd",
            str(run_cwd),
            "--harness",
            "codex",
            "--state-dir",
            str(state_dir),
        ]
        if max_sessions is not None:
            command.extend(["--max-sessions", str(max_sessions)])
        if commit_mode == runner.COMMIT_MODE_PER_SESSION:
            command.append("--commit-per-session")
        elif commit_mode == runner.COMMIT_MODE_END:
            command.append("--commit-at-end")
        elif commit_mode is not None:
            raise ValueError(f"unsupported commit_mode: {commit_mode}")
        if model is not None:
            command.extend(["--model", model])
        if effort is not None:
            command.extend(["--effort", effort])
        if json_output:
            command.append("--json")
        return command

    def start_run_pty(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
        config_home: Path,
        mode: str,
        cwd: Path | None = None,
        commit_mode: str | None = None,
        extra_env: dict[str, str] | None = None,
        fake_step_sleep: str = "0.8",
    ) -> tuple[subprocess.Popen[bytes], int]:
        master_fd, slave_fd = pty.openpty()
        env_extra = {"FAKE_CODEX_STEP_SLEEP": fake_step_sleep}
        if extra_env is not None:
            env_extra.update(extra_env)
        env = self.loop_env(config_home=config_home, mode=mode, extra=env_extra)
        try:
            process = subprocess.Popen(
                self.run_command(
                    plan_dir=plan_dir,
                    state_dir=state_dir,
                    cwd=cwd,
                    commit_mode=commit_mode,
                ),
                cwd=REPO_ROOT,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        os.set_blocking(master_fd, False)

        def cleanup_process() -> None:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            try:
                os.close(master_fd)
            except OSError:
                pass

        self.addCleanup(cleanup_process)
        return process, master_fd

    def read_run_pty_available(self, master_fd: int, output: str) -> str:
        while True:
            readable, _, _ = select.select([master_fd], [], [], 0)
            if not readable:
                return output
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                return output
            except OSError as error:
                if error.errno == errno.EIO:
                    return output
                raise
            if not chunk:
                return output
            output += chunk.decode("utf-8", errors="replace")

    def read_run_pty_until(
        self,
        master_fd: int,
        output: str,
        predicate,
        *,
        process: subprocess.Popen[bytes],
        timeout: float = 5.0,
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.read_run_pty_available(master_fd, output)
            if predicate(output):
                return output
            if process.poll() is not None:
                break
            remaining = max(0.0, deadline - time.monotonic())
            select.select([master_fd], [], [], min(0.05, remaining))

        output = self.read_run_pty_available(master_fd, output)
        if predicate(output):
            return output
        self.fail(f"condition was not met before timeout. PTY output:\n{output}")

    def finish_run_pty(
        self,
        *,
        process: subprocess.Popen[bytes],
        master_fd: int,
        output: str,
        timeout: float = 15.0,
    ) -> str:
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda _text: process.poll() is not None,
            process=process,
            timeout=timeout,
        )
        process.wait(timeout=5)
        return self.read_run_pty_available(master_fd, output)

    def read_run_payload(
        self,
        *,
        plan_dir: Path,
        state_dir: Path,
    ) -> dict[str, object]:
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        return json.loads(state_path.read_text(encoding="utf-8"))

    def test_compact_status_snapshot_reports_active_turn(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        active_turn = {
            "session": 1,
            "session_id": "1",
            "total_sessions": 2,
            "turn_index": 1,
            "harness": "codex",
            "stdout_path": "/tmp/run/stdout.log",
            "stderr_path": "/tmp/run/stderr.log",
            "last_message_path": "/tmp/run/last-message.txt",
            "result_path": "/tmp/run/result.json",
            "elapsed_seconds": 12.3,
            "idle_seconds": 2.5,
        }
        payload = {
            "harness": "codex",
            "model": "gpt-5",
            "model_source": config.MODEL_SOURCE_FLAG,
            "effort": "high",
            "effort_source": config.EFFORT_SOURCE_FLAG,
            "active_turn": active_turn,
            "turns": [],
        }
        control = {
            "stop_after_current": True,
            "tail_enabled": True,
            "next_harness": "claude",
        }

        text = "\n".join(
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
            )
        )

        self.assertIn("status: active session 1/2 turn 1", text)
        self.assertIn("sessions 0/2 complete, 2 pending", text)
        self.assertIn("harness active codex; future claude", text)
        self.assertIn("model gpt-5 (flag); effort high (flag)", text)
        self.assertIn("stop-after-current on; live tail on", text)
        self.assertIn("stdout /tmp/run/stdout.log", text)
        self.assertIn("last-message /tmp/run/last-message.txt", text)

    def test_compact_status_snapshot_reports_idle_between_turns(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("complete", "pending")
        )
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [
                {
                    "stdout_path": "/tmp/run/turn-1/stdout.log",
                    "stderr_path": "/tmp/run/turn-1/stderr.log",
                    "last_message_path": "/tmp/run/turn-1/last-message.txt",
                    "result_path": "/tmp/run/turn-1/result.json",
                }
            ],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
            )
        )

        self.assertIn(
            "status: idle; next session 2/2 turn 2; "
            "sessions 1/2 complete, 1 pending",
            text,
        )
        self.assertIn("stdout /tmp/run/turn-1/stdout.log", text)
        self.assertIn("result /tmp/run/turn-1/result.json", text)

    def test_compact_status_snapshot_reports_no_active_turn(self) -> None:
        plan_dir, _state_dir, _config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [],
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=plans.inspect_plan(plan_dir),
            )
        )

        self.assertIn(
            "status: no active turn; next session 1/1 turn 1; "
            "sessions 0/1 complete, 1 pending",
            text,
        )
        self.assertIn("status paths: none yet", text)

    def test_compact_status_snapshot_reports_plan_inspection_error(self) -> None:
        payload = {
            "harness": "codex",
            "model": config.NATIVE_MODEL_DEFAULT,
            "model_source": config.MODEL_SOURCE_UNRESOLVED,
            "effort": None,
            "effort_source": None,
            "active_turn": None,
            "turns": [],
            "initial_snapshot": {
                "counts": {
                    "pending": 1,
                    "complete": 0,
                    "empty": 0,
                    "unknown": 0,
                },
                "next_pending_id": "1",
            },
        }
        control = {
            "stop_after_current": False,
            "tail_enabled": False,
            "next_harness": None,
        }

        text = "\n".join(
            runner._compact_status_lines(
                payload=payload,
                control=control,
                snapshot=None,
                plan_error="missing context.md",
            )
        )

        self.assertIn("status: plan inspection error: missing context.md", text)
        self.assertIn("sessions 0/1 complete, 1 pending", text)

    def test_successful_turn_syncs_derived_status_ledgers(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        (plan_dir / "manifest.md").write_text(
            "\n".join(
                [
                    "# Manifest",
                    "",
                    "| # | Title | Status | File |",
                    "|---|---|---|---|",
                    "| 1 | One | pending | session-1.md |",
                    "| 2 | Two | pending | session-2.md |",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (plan_dir / "context.md").write_text(
            "\n".join(
                [
                    "# Context",
                    "",
                    "| Session | Title | Status |",
                    "|---|---|---|",
                    "| S1 | One | pending |",
                    "| S2 | Two | pending |",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        completed, payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        turn = payload["turns"][0]
        self.assertEqual(
            {
                str(plans.canonicalize_path(plan_dir / "manifest.md")),
                str(plans.canonicalize_path(plan_dir / "context.md")),
            },
            set(turn["ledger_updates"]),
        )
        self.assertIn(
            "| 1 | One | complete | session-1.md |",
            (plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| 2 | Two | pending | session-2.md |",
            (plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| S1 | One | complete |",
            (plan_dir / "context.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "| S2 | Two | pending |",
            (plan_dir / "context.md").read_text(encoding="utf-8"),
        )

    def test_text_run_pty_p_stops_after_current_session(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn("Run complete: 1 session(s) completed", output)
        self.assertIn("Next pending: session 2", output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": True,
                "last_action": "stop-after-current-consumed",
            },
            payload["control"],
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-consumed",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_second_p_cancels_stop_after_current(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current requested" in text,
            process=process,
        )
        os.write(master_fd, b"p")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: stop-after-current canceled" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1, 2], payload["completed_sessions"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": False,
                "last_action": "stop-after-current-canceled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-canceled",
            ],
            [event["action"] for event in control_events],
        )

    def test_text_run_pty_h_prints_one_help_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/1 started" in text,
            process=process,
        )
        os.write(master_fd, b"h")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "controls: p toggle stop-after-current" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertRegex(
            output,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"controls: p toggle stop-after-current "
            r"\(currently off\); s show status; "
            r"t toggle live tail \(currently off\); "
            r"1 codex, 2 claude, 3 copilot select next harness "
            r"\(currently unchanged\); h show help",
        )
        self.assertEqual(1, output.count("controls: p toggle stop-after-current"))
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        control_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual([], control_events)

    def test_text_run_pty_s_and_number_keys_select_future_harness(self) -> None:
        plan_dir, state_dir, config_home, fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        config_path.write_text(
            json.dumps(
                {
                    "codex": {"executable": str(fake_codex)},
                    "claude": {"executable": str(fake_codex)},
                    "copilot": {"executable": str(fake_codex)},
                }
            ),
            encoding="utf-8",
        )
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            fake_step_sleep="2.0",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "status: active session 1/2 turn 1" in text
                and "status: harness active codex; future unchanged" in text
                and "status: controls stop-after-current off; live tail off" in text
            ),
            process=process,
        )

        os.write(master_fd, b"1")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness already codex" in text,
            process=process,
        )
        os.write(master_fd, b"2")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: claude" in text,
            process=process,
        )
        os.write(master_fd, b"3")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: next harness selected: copilot" in text,
            process=process,
        )
        os.write(master_fd, b"s")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "status: harness active codex; future copilot" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "session 2/2 started" in text,
            process=process,
            timeout=8,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
            timeout=20,
        )

        self.assertEqual(0, process.returncode, output)
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": "copilot",
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )
        turns = payload["turns"]
        self.assertEqual(["codex", "copilot"], [turn["harness"] for turn in turns])
        self.assertTrue(turns[0]["argv"][-1].startswith("$session-run "))
        self.assertIn("-p", turns[1]["argv"])
        self.assertTrue(
            turns[1]["argv"][turns[1]["argv"].index("-p") + 1].startswith(
                "/session-run "
            )
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected", "harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["selected_harness"] for event in control_events],
        )
        self.assertEqual(
            ["claude", "copilot"],
            [event["next_harness"] for event in control_events],
        )

    def test_text_run_pty_t_toggles_inline_tail_for_new_log_lines(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME
        process, master_fd = self.start_run_pty(
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            mode="streaming-success",
            fake_step_sleep="1.5",
        )

        output = self.read_run_pty_until(
            master_fd,
            "",
            lambda text: "session 1/2 started" in text,
            process=process,
        )

        def first_turn_logs_ready():
            if not state_path.exists():
                return None
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = payload.get("active_turn")
            if not isinstance(active_turn, dict):
                return None
            stdout_path = Path(active_turn["stdout_path"])
            stderr_path = Path(active_turn["stderr_path"])
            if not stdout_path.exists() or not stderr_path.exists():
                return None
            if (
                "session 1 stdout before completion"
                in stdout_path.read_text(encoding="utf-8")
                and "session 1 stderr before completion"
                in stderr_path.read_text(encoding="utf-8")
            ):
                return active_turn
            return None

        wait_until(first_turn_logs_ready, timeout=3)
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail enabled" in text,
            process=process,
        )
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=1 turn=1 | session 1 stdout after completion" in text
            ),
            process=process,
            timeout=5,
        )

        self.assertNotIn(
            "stdout session=1 turn=1 | session 1 stdout before completion",
            output,
        )
        self.assertNotIn(
            "stderr session=1 turn=1 | session 1 stderr before completion",
            output,
        )

        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: (
                "stdout session=2 turn=2 | session 2 stdout before completion" in text
            ),
            process=process,
            timeout=5,
        )
        os.write(master_fd, b"t")
        output = self.read_run_pty_until(
            master_fd,
            output,
            lambda text: "control: live tail disabled" in text,
            process=process,
        )
        output = self.finish_run_pty(
            process=process,
            master_fd=master_fd,
            output=output,
            timeout=20,
        )

        self.assertEqual(0, process.returncode, output)
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion",
            output,
        )
        self.assertNotIn(
            "stdout session=2 turn=2 | session 2 stdout after completion",
            output,
        )
        self.assertNotIn(
            "stderr session=2 turn=2 | session 2 stderr after completion",
            output,
        )
        payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": False,
                "last_action": "tail-disabled",
            },
            payload["control"],
        )
        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["tail-enabled", "tail-disabled"],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            [True, False],
            [event["tail_enabled"] for event in control_events],
        )

    def test_text_run_pty_commit_per_session_finishes_before_stop_consumed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
                statuses=("pending", "pending")
            )
            process, master_fd = self.start_run_pty(
                plan_dir=plan_dir,
                state_dir=state_dir,
                config_home=config_home,
                mode="streaming-success",
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            output = self.read_run_pty_until(
                master_fd,
                "",
                lambda text: "session 1/2 started" in text,
                process=process,
            )
            os.write(master_fd, b"p")
            output = self.read_run_pty_until(
                master_fd,
                output,
                lambda text: "control: stop-after-current requested" in text,
                process=process,
            )
            output = self.finish_run_pty(
                process=process,
                master_fd=master_fd,
                output=output,
            )
            payload = self.read_run_payload(plan_dir=plan_dir, state_dir=state_dir)
            spec_records = read_jsonl(spec_log)

        self.assertEqual(0, process.returncode, output)
        self.assertIn("commit 1 finished (session 1, status success,", output)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(spec_records))
        event_records = read_jsonl(Path(payload["events_path"]))
        commit_finished = self.event_index(
            event_records,
            events.COMMIT_FINISHED_EVENT,
            session=1,
            scope="session",
        )
        consumed = self.event_index(
            event_records,
            events.RUN_CONTROL_EVENT,
            action="stop-after-current-consumed",
        )
        self.assertLess(commit_finished, consumed)

    def test_run_control_stop_after_current_stops_between_sessions(self) -> None:
        listener = FakeRunControlListener(consume_on_call=1)
        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual(1, listener.start_calls)
        self.assertEqual(1, listener.stop_calls)
        self.assertEqual(1, listener.consume_calls)

    def test_run_control_events_record_actions_and_final_control_payload(self) -> None:
        listener = FakeRunControlListener(
            consume_on_call=1,
            actions_on_start=(
                run_control.RunControlAction(
                    action="request",
                    stop_after_current=True,
                ),
                run_control.RunControlAction(
                    action="cancel",
                    stop_after_current=False,
                ),
                run_control.RunControlAction(
                    action="request",
                    stop_after_current=True,
                ),
            ),
        )

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": True,
                "last_action": "stop-after-current-consumed",
            },
            payload["control"],
        )

        event_records = read_jsonl(Path(payload["events_path"]))
        control_events = [
            record["payload"]
            for record in event_records
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            [
                "stop-after-current-requested",
                "stop-after-current-canceled",
                "stop-after-current-requested",
                "stop-after-current-consumed",
            ],
            [event["action"] for event in control_events],
        )
        self.assertEqual(
            [True, False, True, False],
            [event["stop_after_current"] for event in control_events],
        )
        self.assertEqual(
            [
                "stop-after-current requested",
                "stop-after-current canceled",
                "stop-after-current requested",
                "stop-after-current consumed",
            ],
            [event["message"] for event in control_events],
        )
        for event in control_events:
            self.assertEqual(payload["run_id"], event["run_id"])

        consumed_event = control_events[-1]
        self.assertEqual(1, consumed_event["session"])
        self.assertEqual(1, consumed_event["turn_index"])
        self.assertEqual(2, consumed_event["total_sessions"])
        run_finished = event_records[self.event_index(event_records, "run_finished")]
        self.assertEqual(payload["control"], run_finished["payload"]["control"])

    def test_harness_selection_applies_to_next_session_and_recomputes_native_model(
        self,
    ) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)
        codex_config = home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")

        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(extra_env={"HOME": str(home)})
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertTrue(str(calls[0]["prompt"]).startswith("$session-run "))
        self.assertTrue(str(calls[1]["prompt"]).startswith("/session-run "))
        self.assertIsNone(calls[0]["model"])
        self.assertIsNone(calls[1]["model"])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual("gpt-5-codex", turns[0]["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, turns[0]["model_source"])
        self.assertEqual(config.NATIVE_MODEL_DEFAULT, turns[1]["model"])
        self.assertEqual(config.MODEL_SOURCE_UNRESOLVED, turns[1]["model_source"])
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": "claude",
                "consumed": False,
                "last_action": "harness-selected",
            },
            payload["control"],
        )

        control_events = [
            record["payload"]
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual(
            ["harness-selected"],
            [event["action"] for event in control_events],
        )
        self.assertEqual("codex", control_events[0]["current_harness"])
        self.assertEqual("claude", control_events[0]["selected_harness"])
        self.assertEqual("claude", control_events[0]["next_harness"])
        self.assertEqual(1, control_events[0]["session"])
        self.assertEqual(1, control_events[0]["turn_index"])

    def test_harness_selection_carries_explicit_model_and_effort(self) -> None:
        exit_code, payload, calls, _plan_dir, _state_dir = (
            self.run_with_mocked_harness_switch(model="gpt-5", effort="high")
        )

        self.assertEqual(0, exit_code)
        self.assertEqual(["codex", "claude"], [call["harness"] for call in calls])
        self.assertEqual(["gpt-5", "gpt-5"], [call["model"] for call in calls])
        self.assertEqual(["high", "high"], [call["effort"] for call in calls])

        turns = payload["turns"]
        self.assertEqual(["codex", "claude"], [turn["harness"] for turn in turns])
        self.assertEqual(["gpt-5", "gpt-5"], [turn["model"] for turn in turns])
        self.assertEqual(
            [config.MODEL_SOURCE_FLAG, config.MODEL_SOURCE_FLAG],
            [turn["model_source"] for turn in turns],
        )
        self.assertEqual(["high", "high"], [turn["effort"] for turn in turns])
        self.assertEqual(
            [config.EFFORT_SOURCE_FLAG, config.EFFORT_SOURCE_FLAG],
            [turn["effort_source"] for turn in turns],
        )

    def test_missing_switched_harness_pauses_before_next_turn(self) -> None:
        listener = FakeRunControlListener()
        calls: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as tmp:
            missing_claude = Path(tmp) / "missing-claude"

            def complete_next_pending(plan_dir: Path) -> None:
                for session_file in sorted(plan_dir.glob("session-*.md")):
                    text = session_file.read_text(encoding="utf-8")
                    if "- **Status**: pending" not in text:
                        continue
                    session_file.write_text(
                        text.replace(
                            "- **Status**: pending",
                            "- **Status**: complete",
                            1,
                        ),
                        encoding="utf-8",
                    )
                    return

            def fake_run_harness(
                name: str,
                **kwargs: object,
            ) -> harnesses.HarnessResult:
                calls.append({"harness": name, **kwargs})
                complete_next_pending(Path(kwargs["plan_dir"]))
                if listener.on_action is not None:
                    listener.on_action(
                        run_control.RunControlAction(
                            action="harness-selected",
                            stop_after_current=False,
                            next_harness="claude",
                        )
                    )
                return harnesses.HarnessResult(
                    name=name,
                    argv=[name, str(kwargs["prompt"])],
                    returncode=0,
                    stdout=f"{name} completed\n",
                    stderr="",
                )

            with mock.patch.object(
                runner.harnesses,
                "run_harness",
                side_effect=fake_run_harness,
            ):
                exit_code, payload, plan_dir, state_dir, _fake_codex = (
                    self.run_locked_with_listener(
                        mode="success",
                        statuses=("pending", "pending"),
                        listener=listener,
                        harness_config_overrides={
                            "claude": str(missing_claude),
                        },
                    )
                )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("harness-unavailable", payload["reason"])
        self.assertEqual("harness-unavailable", payload["pause_reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["1"], payload["completed_session_ids"])
        self.assertEqual(1, len(payload["turns"]))
        self.assertEqual("complete", payload["turns"][0]["status_result"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual("2", payload["final_snapshot"]["next_pending_id"])
        self.assertEqual(["codex"], [call["harness"] for call in calls])
        self.assertFalse(state.lock_path_for_plan(plan_dir, state_dir).exists())

        details = payload["pause_details"]
        self.assertEqual(2, details["session"])
        self.assertEqual("2", details["session_id"])
        self.assertEqual("session-2.md", details["session_file"])
        self.assertEqual("claude", details["requested_harness"])
        self.assertEqual(str(missing_claude), details["executable"])
        self.assertIsNone(details["resolved_executable"])
        self.assertEqual(payload["harness_config"]["path"], details["config_path"])
        self.assertIn("claude executable not found", details["error"])

        event_records = read_jsonl(Path(payload["events_path"]))
        self.assertEqual(
            1,
            len(
                [
                    record
                    for record in event_records
                    if record["name"] == "turn_started"
                ]
            ),
        )
        paused_event = event_records[
            self.event_index(
                event_records,
                events.RUN_PAUSED_EVENT,
                reason="harness-unavailable",
            )
        ]
        paused_details = paused_event["payload"]["details"]
        self.assertEqual("claude", paused_details["requested_harness"])
        self.assertEqual(str(missing_claude), paused_details["executable"])

    def test_run_control_starts_after_state_and_event_files_exist(self) -> None:
        listener = FakeRunControlListener(consume_on_call=None)

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(1, listener.start_calls)
        self.assertTrue(listener.state_exists_at_start)
        self.assertTrue(listener.events_exists_at_start)

    def test_run_control_status_callback_prints_without_recording_event(
        self,
    ) -> None:
        listener = FakeRunControlListener(
            status_on_start=(
                run_control.RunControlAction(
                    action="status",
                    stop_after_current=False,
                    tail_enabled=True,
                    next_harness="claude",
                ),
            ),
        )
        stderr = io.StringIO()

        with redirect_stderr(stderr):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending",),
                    listener=listener,
                )
            )

        self.assertEqual(0, exit_code)
        stderr_text = stderr.getvalue()
        self.assertIn(
            "status: no active turn; next session 1/1 turn 1; "
            "sessions 0/1 complete, 1 pending",
            stderr_text,
        )
        self.assertIn("harness active codex; future claude", stderr_text)
        self.assertIn("stop-after-current off; live tail on", stderr_text)
        self.assertEqual(
            {
                "stop_after_current": False,
                "tail_enabled": False,
                "next_harness": None,
                "consumed": False,
                "last_action": None,
            },
            payload["control"],
        )
        control_events = [
            record
            for record in read_jsonl(Path(payload["events_path"]))
            if record["name"] == events.RUN_CONTROL_EVENT
        ]
        self.assertEqual([], control_events)

    def test_run_control_final_session_keeps_all_complete_reason(self) -> None:
        listener = FakeRunControlListener(consume_on_call=1)

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="success",
                statuses=("pending",),
                listener=listener,
            )
        )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        self.assertEqual(1, listener.consume_calls)

    def test_run_control_stop_after_current_waits_for_per_session_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            listener = FakeRunControlListener(consume_on_call=1)

            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending", "pending"),
                    listener=listener,
                    cwd=repo_dir,
                    commit_mode=runner.COMMIT_MODE_PER_SESSION,
                    extra_env=spec_env,
                )
            )
            spec_records = read_jsonl(spec_log)

        self.assertEqual(0, exit_code)
        self.assertEqual("stopped", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(1, len(payload["commits"]))
        self.assertTrue(payload["commits"][0]["success"])
        self.assertEqual(1, len(spec_records))
        event_records = read_jsonl(Path(payload["events_path"]))
        commit_finished = self.event_index(
            event_records,
            events.COMMIT_FINISHED_EVENT,
            session=1,
            scope="session",
        )
        consumed = self.event_index(
            event_records,
            events.RUN_CONTROL_EVENT,
            action="stop-after-current-consumed",
        )
        self.assertLess(commit_finished, consumed)

    def test_run_control_stop_request_does_not_override_harness_failure(self) -> None:
        listener = FakeRunControlListener(consume_on_call=1)

        exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
            self.run_locked_with_listener(
                mode="failure",
                statuses=("pending", "pending"),
                listener=listener,
            )
        )

        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["reason"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual(0, listener.consume_calls)
        self.assertEqual(1, listener.stop_calls)

    def test_successful_fake_harness_turn_persists_state_and_artifacts(self) -> None:
        completed, payload, plan_dir, _state_dir, fake_codex = self.run_loop(
            mode="success",
            statuses=("pending", "pending"),
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual(["codex"], payload["harness_config"]["configured_harnesses"])

        turn = payload["turns"][0]
        self.assertEqual(str(fake_codex), turn["argv"][0])
        self.assertEqual(payload["run_id"], turn["run_id"])
        self.assertEqual(1, turn["turn_index"])
        self.assertEqual(1, turn["session"])
        self.assertEqual(
            Path(payload["state_dir"]) / "runs" / payload["run_id"],
            Path(payload["run_dir"]),
        )
        artifact_dir = Path(turn["artifact_dir"])
        self.assertEqual(
            Path(payload["run_dir"]) / "turn-0001-session-1",
            artifact_dir,
        )
        self.assertEqual(artifact_dir / "stdout.log", Path(turn["stdout_path"]))
        self.assertEqual(artifact_dir / "stderr.log", Path(turn["stderr_path"]))
        self.assertEqual(
            artifact_dir / "last-message.txt",
            Path(turn["last_message_path"]),
        )
        self.assertEqual(artifact_dir / "result.json", Path(turn["result_path"]))
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertTrue(Path(turn[key]).exists(), key)
        events_payloads = [
            json.loads(line)
            for line in Path(payload["events_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        turn_started = next(
            event for event in events_payloads if event["name"] == "turn_started"
        )
        self.assertEqual(payload["run_id"], turn_started["payload"]["run_id"])
        self.assertEqual(1, turn_started["payload"]["turn_index"])
        self.assertEqual(str(artifact_dir), turn_started["payload"]["artifact_dir"])
        turn_finished = next(
            event for event in events_payloads if event["name"] == "turn_finished"
        )
        self.assertEqual(payload["run_id"], turn_finished["payload"]["run_id"])
        self.assertEqual(1, turn_finished["payload"]["turn_index"])
        self.assertEqual(1, turn_finished["payload"]["session"])
        self.assertEqual(str(artifact_dir), turn_finished["payload"]["artifact_dir"])
        for key in ("stdout_path", "stderr_path", "last_message_path", "result_path"):
            self.assertEqual(turn[key], turn_finished["payload"][key])
            self.assertEqual(artifact_dir, Path(turn_finished["payload"][key]).parent)
        self.assertTrue(Path(payload["state_path"]).exists())
        self.assertTrue(Path(payload["events_path"]).exists())
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_dotted_fake_harness_run_auto_advances_nested_sessions(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop_with_ids(
            statuses=(
                ("1", "complete"),
                ("2.1", "pending"),
                ("2.2", "pending"),
                ("3", "pending"),
            )
        )

        completed, payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("all-complete", payload["reason"])
        self.assertEqual(["2.1", "2.2", 3], payload["completed_sessions"])
        self.assertEqual(["2.1", "2.2", "3"], payload["completed_session_ids"])
        self.assertIsNone(payload["final_snapshot"]["next_pending"])
        self.assertIsNone(payload["final_snapshot"]["next_pending_id"])

        turns = payload["turns"]
        self.assertEqual(["2.1", "2.2", 3], [turn["session"] for turn in turns])
        self.assertEqual(["2.1", "2.2", "3"], [turn["session_id"] for turn in turns])
        self.assertNotIn("session_number", turns[0])
        self.assertNotIn("session_number", turns[1])
        self.assertEqual(3, turns[2]["session_number"])
        self.assertEqual(
            [
                Path(payload["run_dir"]) / "turn-0001-session-2.1",
                Path(payload["run_dir"]) / "turn-0002-session-2.2",
                Path(payload["run_dir"]) / "turn-0003-session-3",
            ],
            [Path(turn["artifact_dir"]) for turn in turns],
        )

        event_records = read_jsonl(Path(payload["events_path"]))
        started_ids = [
            record["payload"]["session_id"]
            for record in event_records
            if record["name"] == "turn_started"
        ]
        self.assertEqual(["2.1", "2.2", "3"], started_ids)

    def test_fake_codex_explicit_dotted_session_prompt_marks_selected_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            write_plan_with_ids(
                plan_dir,
                (("2", "pending"), ("2.1", "pending"), ("3", "pending")),
            )
            fake_codex = install_fake_codex(tmp_dir)
            last_message = tmp_dir / "last-message.txt"

            completed = subprocess.run(
                [
                    str(fake_codex),
                    "exec",
                    "--add-dir",
                    str(plan_dir),
                    "-o",
                    str(last_message),
                    f"$session-run {plan_dir}/ --session 2.1",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-2.1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-3.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Runner status: complete",
                last_message.read_text(encoding="utf-8"),
            )

    def test_unexpected_dotted_progress_reports_expected_and_observed_ids(
        self,
    ) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop_with_ids(
            statuses=(("2.1", "pending"), ("2.2", "pending"))
        )

        completed, payload = self.run_loop_command(
            mode="unexpected-progress",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        expected = [{"before": "pending", "after": "complete", "id": "2.1"}]
        observed = [{"before": "pending", "after": "complete", "id": "2.2"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual("2.1", payload["final_snapshot"]["next_pending"])
        self.assertEqual("2.1", payload["final_snapshot"]["next_pending_id"])
        self.assertEqual([], payload["completed_sessions"])
        self.assertEqual([], payload["completed_session_ids"])
        self.assertEqual("2.1", payload["turns"][0]["session_id"])
        self.assertEqual(
            "unexpected-status-change",
            payload["turns"][0]["status_result"],
        )
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-2.1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.2.md").read_text(encoding="utf-8"),
        )

    def test_integer_fake_harness_run_keeps_numeric_payload_contract(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending", "pending"),
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual([1], payload["completed_sessions"])
        self.assertEqual(["1"], payload["completed_session_ids"])
        self.assertEqual(2, payload["final_snapshot"]["next_pending"])
        self.assertEqual("2", payload["final_snapshot"]["next_pending_id"])
        turn = payload["turns"][0]
        self.assertEqual(1, turn["session"])
        self.assertEqual("1", turn["session_id"])
        self.assertEqual(1, turn["session_number"])
        self.assertEqual(
            Path(payload["run_dir"]) / "turn-0001-session-1",
            Path(turn["artifact_dir"]),
        )

    def test_json_run_reports_explicit_model_effort_metadata_and_argv(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending",),
            max_sessions=1,
            model="gpt-5",
            effort="high",
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("gpt-5", payload["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, payload["model_source"])
        self.assertEqual("high", payload["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, payload["effort_source"])

        turn = payload["turns"][0]
        self.assertEqual("gpt-5", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, turn["model_source"])
        self.assertEqual("high", turn["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, turn["effort_source"])
        self.assertEqual("gpt-5", turn["argv"][turn["argv"].index("--model") + 1])
        self.assertEqual(
            'model_reasoning_effort="high"',
            turn["argv"][turn["argv"].index("-c") + 1],
        )

        result_payload = json.loads(
            Path(turn["result_path"]).read_text(encoding="utf-8")
        )
        self.assertEqual("gpt-5", result_payload["model"])
        self.assertEqual(config.MODEL_SOURCE_FLAG, result_payload["model_source"])
        self.assertEqual("high", result_payload["effort"])
        self.assertEqual(config.EFFORT_SOURCE_FLAG, result_payload["effort_source"])

        event_payloads = [
            json.loads(line)
            for line in Path(payload["events_path"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        for event_name in ("run_started", "turn_started", "turn_finished"):
            with self.subTest(event_name=event_name):
                event_payload = next(
                    event["payload"]
                    for event in event_payloads
                    if event["name"] == event_name
                )
                self.assertEqual("gpt-5", event_payload["model"])
                self.assertEqual(
                    config.MODEL_SOURCE_FLAG, event_payload["model_source"]
                )
                self.assertEqual("high", event_payload["effort"])
                self.assertEqual(
                    config.EFFORT_SOURCE_FLAG, event_payload["effort_source"]
                )

    def test_json_run_reports_native_model_metadata_without_argv_flags(self) -> None:
        home_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(home_tmp.cleanup)
        home = Path(home_tmp.name)
        codex_config = home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True)
        codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")

        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="success",
            statuses=("pending",),
            max_sessions=1,
            extra_env={"HOME": str(home)},
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("gpt-5-codex", payload["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, payload["model_source"])
        self.assertIsNone(payload["effort"])
        self.assertIsNone(payload["effort_source"])

        turn = payload["turns"][0]
        self.assertEqual("gpt-5-codex", turn["model"])
        self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, turn["model_source"])
        self.assertIsNone(turn["effort"])
        self.assertIsNone(turn["effort_source"])
        self.assertNotIn("--model", turn["argv"])
        self.assertNotIn("-c", turn["argv"])

    def test_commit_preflight_rejects_dirty_initial_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            (repo_dir / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-preflight", payload["pause_reason"])
            self.assertEqual("commit-preflight", payload["reason"])
            self.assertIn("git status --porcelain", payload["pause_message"])
            self.assertEqual([], payload["turns"])
            self.assertEqual([], payload["commits"])
            self.assertFalse(spec_log.exists())
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )

    def test_commit_per_session_commits_after_each_completed_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(runner.COMMIT_MODE_PER_SESSION, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count + 2,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            expected_cwd = str(plans.canonicalize_path(repo_dir))
            spec_records = read_jsonl(spec_log)
            self.assertEqual(2, len(spec_records))
            for record in spec_records:
                self.assertEqual(["commit", "--yes", "--no-verify"], record["argv"][1:])
                self.assertEqual(expected_cwd, record["cwd"])

            self.assertEqual(2, len(payload["commits"]))
            for expected_session, commit in enumerate(payload["commits"], start=1):
                self.assertTrue(commit["success"])
                self.assertEqual("success", commit["status_result"])
                self.assertEqual("session", commit["scope"])
                self.assertEqual(expected_session, commit["session"])
                self.assertEqual(expected_cwd, commit["cwd"])
                self.assertEqual(
                    ["spec", "commit", "--yes", "--no-verify"],
                    commit["argv"],
                )
                self.assertTrue(Path(commit["result_path"]).exists())

            event_records = read_jsonl(Path(payload["events_path"]))
            turn_1_finished = self.event_index(
                event_records, "turn_finished", session=1
            )
            commit_1_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=1,
                scope="session",
            )
            commit_1_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=1,
                scope="session",
            )
            turn_2_started = self.event_index(event_records, "turn_started", session=2)
            self.assertLess(turn_1_finished, commit_1_started)
            self.assertLess(commit_1_started, commit_1_finished)
            self.assertLess(commit_1_finished, turn_2_started)

    def test_commit_at_end_commits_once_after_all_sessions_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(tmp_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_END,
                extra_env=spec_env,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertEqual(runner.COMMIT_MODE_END, payload["commit_mode"])
            self.assertEqual([1, 2], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))
            self.assertEqual(
                initial_commit_count + 1,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            commit = payload["commits"][0]
            self.assertTrue(commit["success"])
            self.assertEqual("end", commit["scope"])
            self.assertIsNone(commit["session"])

            event_records = read_jsonl(Path(payload["events_path"]))
            turn_2_finished = self.event_index(
                event_records, "turn_finished", session=2
            )
            commit_started = self.event_index(
                event_records,
                events.COMMIT_STARTED_EVENT,
                session=None,
                scope="end",
            )
            commit_finished = self.event_index(
                event_records,
                events.COMMIT_FINISHED_EVENT,
                session=None,
                scope="end",
            )
            run_finished = self.event_index(event_records, "run_finished")
            self.assertLess(turn_2_finished, commit_started)
            self.assertLess(commit_started, commit_finished)
            self.assertLess(commit_finished, run_finished)

    def test_commit_failure_pauses_with_failed_commit_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(
                tmp_dir,
                mode="failure",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending", "pending"),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-failed", payload["pause_reason"])
            self.assertEqual([], payload["completed_sessions"])
            self.assertEqual(1, len(payload["turns"]))
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))
            self.assertEqual(initial_head, git_stdout(repo_dir, "rev-parse", "HEAD"))

            commit = payload["commits"][0]
            self.assertFalse(commit["success"])
            self.assertEqual("nonzero-exit", commit["status_result"])
            self.assertEqual(23, commit["returncode"])
            self.assertEqual(commit, payload["pause_details"]["commit"])
            self.assertIn(
                "- **Status**: complete",
                (plan_dir / "session-1.md").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- **Status**: pending",
                (plan_dir / "session-2.md").read_text(encoding="utf-8"),
            )

    def test_unchanged_head_commit_pauses_with_commit_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            repo_dir = tmp_dir / "repo"
            init_git_repo(repo_dir)
            _fake_spec, spec_log, spec_env = self.setup_fake_spec(
                tmp_dir,
                mode="unchanged-head",
            )
            initial_head = git_stdout(repo_dir, "rev-parse", "HEAD")

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
                commit_mode=runner.COMMIT_MODE_PER_SESSION,
                extra_env=spec_env,
            )

            self.assertEqual(1, completed.returncode)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["paused"])
            self.assertEqual("commit-failed", payload["pause_reason"])
            self.assertEqual([], payload["completed_sessions"])
            self.assertEqual(1, len(payload["commits"]))
            self.assertEqual(1, len(read_jsonl(spec_log)))

            commit = payload["commits"][0]
            self.assertFalse(commit["success"])
            self.assertEqual("unchanged-head", commit["status_result"])
            self.assertEqual(0, commit["returncode"])
            self.assertEqual(initial_head, commit["head_before"])
            self.assertEqual(initial_head, commit["head_after"])
            self.assertIsNone(commit["commit_sha"])

    def test_default_run_does_not_require_or_create_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            init_git_repo(repo_dir)
            initial_commit_count = int(
                git_stdout(repo_dir, "rev-list", "--count", "HEAD")
            )

            completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
                mode="success",
                statuses=("pending",),
                cwd=repo_dir,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("all-complete", payload["reason"])
            self.assertIsNone(payload["commit_mode"])
            self.assertEqual([], payload["commits"])
            self.assertEqual([1], payload["completed_sessions"])
            self.assertEqual(
                initial_commit_count,
                int(git_stdout(repo_dir, "rev-list", "--count", "HEAD")),
            )

            event_names = [
                record["name"] for record in read_jsonl(Path(payload["events_path"]))
            ]
            self.assertNotIn(events.COMMIT_STARTED_EVENT, event_names)
            self.assertNotIn(events.COMMIT_FINISHED_EVENT, event_names)

    def test_text_run_emits_human_progress_to_stderr(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.05"},
            ),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Plan:", completed.stdout)
        self.assertIn("Completed session 1", completed.stdout)
        self.assertIn("Run complete: 1 session(s) completed", completed.stdout)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            completed.stderr,
            timestamp + r"run started: harness codex; run ",
        )
        self.assertIn("run dir ", completed.stderr)
        self.assertIn("state ", completed.stderr)
        self.assertIn("events ", completed.stderr)
        self.assertRegex(
            completed.stderr,
            timestamp + r"session 1/1 started",
        )
        self.assertIn(
            "session 1/1 finished (status complete, return code 0, duration ",
            completed.stderr,
        )
        self.assertIn("run finished: run ", completed.stderr)
        self.assertIn("reason all-complete", completed.stderr)
        self.assertNotIn("stdout=", completed.stderr)
        self.assertNotIn("stderr=", completed.stderr)
        self.assertNotIn("result=", completed.stderr)

    def test_text_run_emits_final_paused_progress_line(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        completed = subprocess.run(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir),
            cwd=REPO_ROOT,
            env=self.loop_env(config_home=config_home, mode="failure"),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(1, completed.returncode)
        self.assertIn("Run paused: nonzero-exit", completed.stderr)
        self.assertRegex(
            completed.stderr,
            r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"run paused: run [^,]+, "
            r"reason nonzero-exit, duration \d+\.\ds",
        )

    def test_json_run_stays_quiet_and_status_reports_active_turn(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.8"},
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def cleanup_run_process() -> None:
            if run_process.poll() is None:
                run_process.kill()
            try:
                run_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                run_process.kill()
                run_process.communicate(timeout=5)

        self.addCleanup(cleanup_run_process)

        state_path = state.plan_state_dir(plan_dir, state_dir) / events.STATE_FILE_NAME

        def active_turn_payload():
            if not state_path.exists():
                return None
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            active_turn = raw.get("active_turn")
            return active_turn if isinstance(active_turn, dict) else None

        active_turn = wait_until(active_turn_payload)
        self.assertEqual(1, active_turn["session"])
        self.assertEqual(1, active_turn["turn_index"])
        self.assertTrue(active_turn["stdout_path"].endswith("/stdout.log"))
        self.assertTrue(active_turn["stderr_path"].endswith("/stderr.log"))

        status_completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--json",
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        status_payload = json.loads(status_completed.stdout)
        self.assertTrue(status_payload["ok"])
        self.assertEqual(1, status_payload["active_turn"]["session"])
        self.assertIn("elapsed_seconds", status_payload["active_turn"])
        self.assertIn("stdout_path", status_payload["active_turn"])

        status_text = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "status",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("Active turn: session 1 turn 1", status_text.stdout)
        self.assertIn("Stdout:", status_text.stdout)
        self.assertIn("Stderr:", status_text.stdout)

        run_stdout, run_stderr = run_process.communicate(timeout=10)
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1], run_payload["completed_sessions"])

    def test_tail_follows_streaming_logs_across_sessions(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending", "pending")
        )
        run_process = subprocess.Popen(
            self.run_command(plan_dir=plan_dir, state_dir=state_dir, json_output=True),
            cwd=REPO_ROOT,
            env=self.loop_env(
                config_home=config_home,
                mode="streaming-success",
                extra={"FAKE_CODEX_STEP_SLEEP": "0.15"},
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: run_process.kill() if run_process.poll() is None else None
        )

        events_path = (
            state.plan_state_dir(plan_dir, state_dir) / events.EVENTS_FILE_NAME
        )

        def has_first_turn_started():
            if not events_path.exists():
                return False
            return "turn_started" in events_path.read_text(encoding="utf-8")

        wait_until(has_first_turn_started)
        tail_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_session_loop",
                "tail",
                str(plan_dir),
                "--state-dir",
                str(state_dir),
                "--lines",
                "200",
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(
            lambda: tail_process.kill() if tail_process.poll() is None else None
        )

        run_stdout, run_stderr = run_process.communicate(timeout=10)
        tail_stdout, tail_stderr = tail_process.communicate(timeout=10)
        self.assertEqual("", run_stderr)
        run_payload = json.loads(run_stdout)
        self.assertTrue(run_payload["ok"])
        self.assertEqual([1, 2], run_payload["completed_sessions"])

        self.assertEqual(0, tail_process.returncode, tail_stderr)
        self.assertEqual("", tail_stderr)
        timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
        self.assertRegex(
            tail_stdout,
            timestamp + r"session 1/2 started",
        )
        self.assertIn("session 1/2 finished", tail_stdout)
        self.assertIn("session 2/2 started", tail_stdout)
        self.assertIn("run finished: run ", tail_stdout)
        self.assertNotIn("event turn_started", tail_stdout)
        self.assertIn(
            "stdout session=1 turn=1 | session 1 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=1 turn=1 | session 1 stderr before completion", tail_stdout
        )
        self.assertIn(
            "stdout session=2 turn=2 | session 2 stdout before completion", tail_stdout
        )
        self.assertIn(
            "stderr session=2 turn=2 | session 2 stderr after completion", tail_stdout
        )

    def test_live_turn_heartbeat_updates_state_events_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {"state_path": str(state_path), "active_turn": None}
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 3,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            progress = runner._ProgressReporter(enabled=True)
            stderr = io.StringIO()

            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=progress,
                active_turn=active_turn,
                heartbeat_interval=0.01,
            )
            with redirect_stderr(stderr):
                live_turn.start()
                wait_until(
                    lambda: (
                        event_path.exists()
                        and events.TURN_HEARTBEAT_EVENT
                        in event_path.read_text(encoding="utf-8")
                    )
                )
                live_turn.stop()
                live_turn.finish(runner._utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            event_records = [
                json.loads(line)
                for line in event_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIsNone(state_payload["active_turn"])
            self.assertTrue(
                any(
                    record["name"] == events.TURN_HEARTBEAT_EVENT
                    for record in event_records
                )
            )
            stderr_text = stderr.getvalue()
            timestamp = r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 started",
            )
            self.assertRegex(
                stderr_text,
                timestamp + r"session 1/3 heartbeat \(elapsed",
            )

    def test_run_persistence_serializes_live_turn_and_control_state_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            event_path = tmp_dir / events.EVENTS_FILE_NAME
            state_path = tmp_dir / events.STATE_FILE_NAME
            payload = {
                "state_path": str(state_path),
                "run_id": "run-1",
                "harness": "codex",
                "active_turn": None,
                "control": runner._initial_control_payload(),
            }
            active_turn = {
                "run_id": "run-1",
                "turn_index": 1,
                "session": 1,
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": runner._isoformat(runner._utc_now()),
                "finished_at": None,
                "duration_seconds": None,
                "idle_seconds_since_previous_turn": 0.0,
                "last_activity_at": None,
                "last_activity_stream": None,
                "last_activity_preview": "",
                "last_heartbeat_at": None,
                "elapsed_seconds": 0.0,
                "idle_seconds": 0.0,
                "heartbeat_count": 0,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "action_required": None,
            }
            persistence = runner._RunPersistence()
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                persistence=persistence,
                heartbeat_interval=60,
            )
            activity_write_started = threading.Event()
            release_activity_write = threading.Event()
            delayed_once = False
            thread_errors: list[BaseException] = []
            original_write_json = runner._write_json_file

            def delayed_write_json(path: Path, payload_arg: dict[str, object]) -> None:
                nonlocal delayed_once
                path = Path(path)
                active = payload_arg.get("active_turn")
                control = payload_arg.get("control")
                if (
                    path == state_path
                    and not delayed_once
                    and isinstance(active, dict)
                    and active.get("last_activity_stream") == "stdout"
                    and isinstance(control, dict)
                    and control.get("next_harness") is None
                ):
                    delayed_once = True
                    serialized = json.dumps(payload_arg, sort_keys=True, indent=2) + "\n"
                    activity_write_started.set()
                    release_activity_write.wait(timeout=2)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = path.with_name(f".{path.name}.blocked.tmp")
                    temporary_path.write_text(serialized, encoding="utf-8")
                    os.replace(temporary_path, path)
                    return
                original_write_json(path, payload_arg)

            def record_activity() -> None:
                try:
                    live_turn.record_activity("stdout", "new output\n")
                except BaseException as error:
                    thread_errors.append(error)

            def record_control() -> None:
                try:
                    runner._record_run_control_action(
                        event_path=event_path,
                        payload=payload,
                        control_payload=dict(payload["control"]),
                        action=run_control.RunControlAction(
                            action="harness-selected",
                            stop_after_current=False,
                            next_harness="claude",
                        ),
                        context=active_turn,
                        persistence=persistence,
                    )
                except BaseException as error:
                    thread_errors.append(error)

            with mock.patch.object(
                runner,
                "_write_json_file",
                side_effect=delayed_write_json,
            ):
                activity_thread = threading.Thread(target=record_activity)
                activity_thread.start()
                self.assertTrue(activity_write_started.wait(timeout=2))

                control_thread = threading.Thread(target=record_control)
                control_thread.start()
                time.sleep(0.1)
                self.assertTrue(control_thread.is_alive())

                release_activity_write.set()
                activity_thread.join(timeout=2)
                control_thread.join(timeout=2)

            self.assertFalse(activity_thread.is_alive())
            self.assertFalse(control_thread.is_alive())
            if thread_errors:
                raise thread_errors[0]

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("stdout", state_payload["active_turn"]["last_activity_stream"])
            self.assertEqual("claude", state_payload["control"]["next_harness"])
            self.assertEqual("harness-selected", state_payload["control"]["last_action"])
            control_events = [
                record["payload"]
                for record in read_jsonl(event_path)
                if record["name"] == events.RUN_CONTROL_EVENT
            ]
            self.assertEqual(["harness-selected"], [event["action"] for event in control_events])

    def test_retry_after_failed_turn_keeps_first_run_artifacts(self) -> None:
        plan_dir, state_dir, config_home, _fake_codex = self.setup_loop(
            statuses=("pending",)
        )

        first_completed, first_payload = self.run_loop_command(
            mode="failure",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
        )

        self.assertEqual(1, first_completed.returncode)
        self.assertFalse(first_payload["ok"])
        first_turn = first_payload["turns"][0]
        first_stderr = Path(first_turn["stderr_path"])
        first_result = Path(first_turn["result_path"])
        first_artifact_dir = Path(first_turn["artifact_dir"])
        self.assertEqual(
            Path(first_payload["run_dir"]) / "turn-0001-session-1",
            first_artifact_dir,
        )
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        original_stderr = first_stderr.read_text(encoding="utf-8")
        original_result = json.loads(first_result.read_text(encoding="utf-8"))
        self.assertIn("fake codex failure", original_stderr)
        self.assertEqual(7, original_result["returncode"])
        self.assertEqual(first_payload["run_id"], original_result["run_id"])

        second_completed, second_payload = self.run_loop_command(
            mode="success",
            plan_dir=plan_dir,
            state_dir=state_dir,
            config_home=config_home,
            max_sessions=1,
        )

        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertNotEqual(first_payload["run_id"], second_payload["run_id"])
        second_turn = second_payload["turns"][0]
        self.assertNotEqual(first_artifact_dir, Path(second_turn["artifact_dir"]))
        self.assertTrue(first_stderr.exists())
        self.assertTrue(first_result.exists())
        self.assertEqual(original_stderr, first_stderr.read_text(encoding="utf-8"))
        self.assertEqual(
            original_result,
            json.loads(first_result.read_text(encoding="utf-8")),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )

    def test_nonzero_fake_harness_pauses_without_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="failure",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("nonzero-exit", payload["pause_reason"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)

    def test_timed_out_fake_harness_reports_timeout_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="timeout",
            statuses=("pending",),
            timeout=1,
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("timeout", payload["pause_reason"])
        self.assertTrue(payload["turns"][0]["timed_out"])

    def test_bad_fake_harness_progress_reports_multiple_status_changes(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="multiple-progress",
            statuses=("pending", "pending"),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("multiple-status-changes", payload["pause_reason"])
        self.assertEqual(2, len(payload["observed_status_changes"]))

    def test_no_progress_fake_harness_reports_no_status_change(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="no-progress",
            statuses=("pending",),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("no-status-change", payload["pause_reason"])
        self.assertEqual("no-status-change", payload["reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual([], payload["observed_status_changes"])
        self.assertEqual(payload["pause_details"], payload["details"])
        self.assertEqual(expected, payload["details"]["expected_status_changes"])
        self.assertEqual([], payload["details"]["observed_status_changes"])

    def test_unexpected_progress_reports_unexpected_status_change(self) -> None:
        completed, payload, plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="unexpected-progress",
            statuses=("pending", "pending"),
        )

        expected = [{"number": 1, "before": "pending", "after": "complete"}]
        observed = [{"number": 2, "before": "pending", "after": "complete"}]
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unexpected-status-change", payload["pause_reason"])
        self.assertEqual(expected, payload["expected_status_changes"])
        self.assertEqual(observed, payload["observed_status_changes"])
        self.assertEqual(observed, payload["details"]["observed_status_changes"])
        self.assertEqual(1, payload["final_snapshot"]["next_pending"])
        self.assertIn(
            "- **Status**: pending",
            (plan_dir / "session-1.md").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "- **Status**: complete",
            (plan_dir / "session-2.md").read_text(encoding="utf-8"),
        )

    def test_runner_pause_signal_reports_runner_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("runner-pause", payload["pause_reason"])
        self.assertEqual("operator requested pause", payload["pause_message"])
        self.assertEqual({"session": 1, "source": "last_message"}, payload["details"])
        turn = payload["turns"][0]
        self.assertEqual("operator requested pause", turn["runner_pause"])
        self.assertEqual("last_message", turn["runner_pause_source"])

    def test_malformed_after_turn_reports_malformed_plan_pause(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="malformed-after-turn",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-plan", payload["pause_reason"])
        self.assertEqual(
            "plan could not be inspected after the harness turn",
            payload["pause_message"],
        )
        self.assertEqual(1, payload["details"]["session"])
        self.assertIn("missing context.md", payload["details"]["error"])
        turn = payload["turns"][0]
        self.assertIsNone(turn["after"])
        self.assertIn("missing context.md", turn["plan_error"])


class JsonCliTests(unittest.TestCase):
    def test_status_and_dry_run_json_outputs_include_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            status_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )
            status_payload = json.loads(status_completed.stdout)
            self.assertTrue(status_payload["ok"])
            self.assertEqual("status", status_payload["command"])
            self.assertEqual(1, status_payload["next_pending"])
            self.assertEqual("1", status_payload["next_pending_id"])
            self.assertEqual("1", status_payload["sessions"][0]["id"])
            self.assertEqual(1, status_payload["sessions"][0]["number"])
            self.assertTrue(status_payload["state_path"].endswith("/state.json"))
            self.assertTrue(status_payload["events_path"].endswith("/events.jsonl"))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            run_completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--model",
                    "gpt-5",
                    "--effort",
                    "high",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )
            run_payload = json.loads(run_completed.stdout)
            self.assertTrue(run_payload["ok"])
            self.assertEqual("run", run_payload["command"])
            self.assertEqual({}, run_payload["details"])
            self.assertEqual("dry-run", run_payload["reason"])
            self.assertIn("harness_config", run_payload)
            self.assertEqual("gpt-5", run_payload["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, run_payload["model_source"])
            self.assertEqual("high", run_payload["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, run_payload["effort_source"])
            self.assertEqual(1, run_payload["planned"]["next_pending"])
            self.assertEqual("1", run_payload["planned"]["next_pending_id"])
            self.assertEqual("1", run_payload["initial_snapshot"]["next_pending_id"])
            planned = run_payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, planned["model_source"])
            self.assertEqual("high", planned["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, planned["effort_source"])
            self.assertEqual("gpt-5", invocation["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, invocation["model_source"])
            self.assertEqual("high", invocation["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, invocation["effort_source"])
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                invocation["argv"],
            )
            self.assertNotIn("workspace-write", invocation["argv"])
            self.assertEqual(
                "gpt-5", invocation["argv"][invocation["argv"].index("--model") + 1]
            )
            self.assertEqual(
                'model_reasoning_effort="high"',
                invocation["argv"][invocation["argv"].index("-c") + 1],
            )
            self.assertTrue(run_payload["state_path"].endswith("/state.json"))
            self.assertTrue(run_payload["events_path"].endswith("/events.jsonl"))

    def test_status_and_dry_run_json_outputs_nested_session_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan_with_ids(plan_dir, (("1", "complete"), ("1.1", "pending")))

            status_payload = state.status_payload(plans.inspect_plan(plan_dir), state_dir)
            self.assertEqual("1.1", status_payload["next_pending"])
            self.assertEqual("1.1", status_payload["next_pending_id"])
            integer_session = status_payload["sessions"][0]
            nested_session = status_payload["sessions"][1]
            self.assertEqual("1", integer_session["id"])
            self.assertEqual(1, integer_session["number"])
            self.assertEqual("1.1", nested_session["id"])
            self.assertNotIn("number", nested_session)

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending"])
            self.assertEqual("1.1", payload["initial_snapshot"]["next_pending_id"])
            self.assertEqual("1.1", payload["planned"]["next_pending"])
            self.assertEqual("1.1", payload["planned"]["next_pending_id"])
            nested = payload["initial_snapshot"]["sessions"][1]
            self.assertEqual("1.1", nested["id"])
            self.assertNotIn("number", nested)

    def test_dry_run_json_no_unsafe_bypass_uses_harness_default_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                    "--no-unsafe-bypass",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            argv = payload["planned"]["invocation"]["argv"]
            self.assertIn("-s", argv)
            self.assertIn("workspace-write", argv)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_run_json_uses_current_directory_when_cwd_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["planned"]["invocation"]
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(REPO_ROOT), invocation["cwd"])
            self.assertEqual(
                str(REPO_ROOT), invocation["argv"][invocation["argv"].index("-C") + 1]
            )

    def test_dry_run_json_reports_native_model_metadata_without_argv_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            home = tmp_dir / "home"
            codex_config = home / ".codex" / "config.toml"
            codex_config.parent.mkdir(parents=True)
            codex_config.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["XDG_CONFIG_HOME"] = str(tmp_dir / "config")
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(REPO_ROOT),
                    "--state-dir",
                    str(state_dir),
                    "--dry-run",
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("gpt-5-codex", payload["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, payload["model_source"])
            self.assertIsNone(payload["effort"])
            self.assertIsNone(payload["effort_source"])
            planned = payload["planned"]
            invocation = planned["invocation"]
            self.assertEqual("gpt-5-codex", planned["model"])
            self.assertEqual(config.MODEL_SOURCE_CODEX_CONFIG, planned["model_source"])
            self.assertIsNone(planned["effort"])
            self.assertIsNone(planned["effort_source"])
            self.assertEqual("gpt-5-codex", invocation["model"])
            self.assertEqual(
                config.MODEL_SOURCE_CODEX_CONFIG, invocation["model_source"]
            )
            self.assertIsNone(invocation["effort"])
            self.assertIsNone(invocation["effort_source"])
            self.assertNotIn("--model", invocation["argv"])
            self.assertNotIn("-c", invocation["argv"])

    def test_status_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "status",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("status", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])

    def test_doctor_json_outputs_harness_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--model",
                    "gpt-5",
                    "--effort",
                    "high",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(fake_codex), payload["resolved_executable"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)), payload["plan_dir"]
            )
            self.assertEqual(1, payload["next_pending"])
            self.assertEqual("gpt-5", payload["model"])
            self.assertEqual(config.MODEL_SOURCE_FLAG, payload["model_source"])
            self.assertEqual("high", payload["effort"])
            self.assertEqual(config.EFFORT_SOURCE_FLAG, payload["effort_source"])
            self.assertTrue(payload["prompt"].startswith("$session-run "))
            self.assertEqual(
                ["codex"], payload["harness_config"]["configured_harnesses"]
            )
            self.assertEqual(str(fake_codex), payload["invocation"]["argv"][0])
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                payload["invocation"]["argv"],
            )
            self.assertNotIn("workspace-write", payload["invocation"]["argv"])
            self.assertEqual(payload["invocation"]["argv"], payload["invocation_argv"])
            self.assertEqual("gpt-5", payload["invocation"]["model"])
            self.assertEqual(
                config.MODEL_SOURCE_FLAG, payload["invocation"]["model_source"]
            )
            self.assertEqual("high", payload["invocation"]["effort"])
            self.assertEqual(
                config.EFFORT_SOURCE_FLAG, payload["invocation"]["effort_source"]
            )
            self.assertEqual(
                "gpt-5",
                payload["invocation_argv"][
                    payload["invocation_argv"].index("--model") + 1
                ],
            )
            self.assertEqual(
                'model_reasoning_effort="high"',
                payload["invocation_argv"][payload["invocation_argv"].index("-c") + 1],
            )

    def test_doctor_json_uses_current_directory_when_cwd_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            invocation = payload["invocation"]
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(REPO_ROOT), invocation["cwd"])
            self.assertEqual(
                str(REPO_ROOT), invocation["argv"][invocation["argv"].index("-C") + 1]
            )

    def test_doctor_text_outputs_model_effort_and_argv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            config_path.write_text(
                json.dumps({"codex": {"executable": str(fake_codex)}}),
                encoding="utf-8",
            )
            write_plan(plan_dir, ("pending",))

            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--model",
                    "gpt-5",
                    "--effort",
                    "high",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                ],
                cwd=REPO_ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=True,
            )

            lines = completed.stdout.splitlines()
            self.assertIn("Model: gpt-5 (flag)", lines)
            self.assertIn("Effort: high (flag)", lines)
            self.assertIn("Argv:", lines)
            model_index = lines.index("  --model")
            effort_index = lines.index("  -c")
            self.assertEqual("  gpt-5", lines[model_index + 1])
            self.assertEqual('  model_reasoning_effort="high"', lines[effort_index + 1])

    def test_doctor_json_error_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "doctor",
                    "--harness",
                    "codex",
                    "--cwd",
                    str(REPO_ROOT),
                    "--plan-dir",
                    str(plan_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("doctor", payload["command"])
            self.assertEqual("malformed-plan", payload["reason"])
            self.assertIn("missing context.md", payload["error"])
            self.assertEqual({}, payload["details"])
            self.assertEqual("codex", payload["harness"])
            self.assertEqual(str(REPO_ROOT), payload["cwd"])
            self.assertEqual(str(plan_dir), payload["plan_dir"])

    def test_unlock_json_success_reports_lock_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "unlock",
                    str(plan_dir),
                    "--state-dir",
                    str(state_dir),
                    "--json",
                ],
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=True,
            )

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual("unlock", payload["command"])
            self.assertFalse(payload["had_lock"])
            self.assertFalse(payload["removed"])
            self.assertFalse(payload["active"])
            self.assertFalse(payload["stale"])
            self.assertTrue(payload["lock_path"].endswith("/lock.json"))
            self.assertIn("no lock present", payload["message"])

    def test_unlock_json_active_lock_failure_goes_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            state_dir = Path(tmp) / "state"
            write_plan(plan_dir, ("pending",))
            lock_info = state.acquire_lock(plan_dir, state_dir)

            try:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "agent_session_loop",
                        "unlock",
                        str(plan_dir),
                        "--state-dir",
                        str(state_dir),
                        "--json",
                    ],
                    cwd=REPO_ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                state.release_lock(plan_dir, state_dir)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["paused"])
            self.assertEqual("unlock", payload["command"])
            self.assertEqual("active-lock", payload["reason"])
            self.assertIn("active lock", payload["error"])
            self.assertEqual(str(lock_info.path), payload["lock_path"])
            self.assertEqual(str(lock_info.path), payload["details"]["lock_path"])
            self.assertEqual(
                str(plans.canonicalize_path(plan_dir)),
                payload["details"]["plan_dir"],
            )


if __name__ == "__main__":
    unittest.main()
