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
    contracts,
    events,
    harnesses,
    plans,
    preflight,
    run_control,
    runner,
    state,
    tail,
    verification,
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


def write_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for number, status_value in enumerate(statuses, start=1):
        contract_block = contracts_by_id.get(str(number), "")
        body = [
            f"# Session {number}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / f"session-{number}.md").write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def write_plan_with_ids(
    plan_dir: Path,
    statuses: tuple[tuple[str, str], ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> None:
    plan_dir.mkdir(parents=True, exist_ok=True)
    (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
    (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
    contracts_by_id = contracts_by_id or {}
    for session_id, status_value in statuses:
        display_label = plans.session_id_display_label(session_id)
        contract_block = contracts_by_id.get(session_id, "")
        body = [
            f"# {display_label}: Test",
            "",
            "Instructions.",
            "",
        ]
        if contract_block:
            body.extend([contract_block, ""])
        body.extend(
            [
                "## Handoff",
                "",
                f"- **Status**: {status_value}",
                "- **Files modified**: (none)",
                "- **Key decisions**: (none)",
                "- **Blockers for next session**: (none)",
                "- **Notes**:",
                "",
            ]
        )
        (plan_dir / plans.session_id_to_filename(session_id)).write_text(
            "\n".join(body),
            encoding="utf-8",
        )


def install_fake_codex(tmp_dir: Path) -> Path:
    return install_fake_harness(tmp_dir, "codex")


def install_fake_harness(tmp_dir: Path, executable_name: str) -> Path:
    fake_path = tmp_dir / "codex"
    if executable_name != "codex":
        fake_path = tmp_dir / executable_name
    shutil.copyfile(FAKE_CODEX_FIXTURE, fake_path)
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path


def install_fake_amx(tmp_dir: Path) -> tuple[Path, Path]:
    fake_path = tmp_dir / "amx"
    log_path = tmp_dir / "fake-amx.jsonl"
    fake_path.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "from pathlib import Path",
                "import json",
                "import os",
                "import sys",
                "",
                "log_path = os.environ.get('FAKE_AMX_LOG')",
                "if log_path:",
                "    Path(log_path).parent.mkdir(parents=True, exist_ok=True)",
                "    with Path(log_path).open('a', encoding='utf-8') as handle:",
                "        handle.write(json.dumps({",
                "            'argv': sys.argv,",
                "            'cwd': os.getcwd(),",
                "            'stdin': sys.stdin.read(),",
                "        }, separators=(',', ':')) + '\\n')",
                "raise SystemExit(0)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_path.chmod(fake_path.stat().st_mode | stat.S_IXUSR)
    return fake_path, log_path


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

    def test_readiness_states_cover_full_lifecycle(self) -> None:
        self.assertEqual(
            ("draft", "reviewed", "loop-ready", "running", "complete"),
            plans.READINESS_STATES,
        )
        for state_value in plans.READINESS_STATES:
            with self.subTest(state=state_value):
                self.assertEqual(state_value, plans.parse_readiness_state(state_value))
        self.assertEqual("loop-ready", plans.parse_readiness_state("  LOOP-READY  "))
        for invalid in ("", "ready", "loop_ready", "DONE"):
            with self.subTest(value=invalid):
                with self.assertRaises(plans.PlanError):
                    plans.parse_readiness_state(invalid)

    def test_parse_readiness_metadata_reads_all_three_lines(self) -> None:
        manifest_text = "\n".join(
            [
                "# Plan",
                "",
                "**Sessions**: 2",
                "**Readiness**: reviewed",
                "**Readiness SHA-256**: `abc123`",
                "**Readiness checked at**: 2026-06-19T12:34:56Z",
                "",
                "## Sessions",
                "",
            ]
        )

        metadata = plans.parse_readiness_metadata(manifest_text)

        self.assertIsNotNone(metadata)
        self.assertEqual("reviewed", metadata.state)
        self.assertEqual("abc123", metadata.readiness_sha256)
        self.assertEqual("2026-06-19T12:34:56Z", metadata.checked_at)

    def test_parse_readiness_metadata_returns_none_when_absent(self) -> None:
        manifest_text = "# Plan\n\n**Sessions**: 1\n\n## Sessions\n"

        self.assertIsNone(plans.parse_readiness_metadata(manifest_text))

    def test_parse_readiness_metadata_rejects_partial_or_invalid_blocks(self) -> None:
        cases = (
            (
                "missing state",
                "**Readiness SHA-256**: `abc`\n**Readiness checked at**: now\n",
            ),
            (
                "missing sha",
                "**Readiness**: draft\n**Readiness checked at**: now\n",
            ),
            (
                "missing checked_at",
                "**Readiness**: draft\n**Readiness SHA-256**: `abc`\n",
            ),
            (
                "invalid state",
                "**Readiness**: green\n"
                "**Readiness SHA-256**: `abc`\n"
                "**Readiness checked at**: now\n",
            ),
            (
                "empty sha",
                "**Readiness**: draft\n"
                "**Readiness SHA-256**: ``\n"
                "**Readiness checked at**: now\n",
            ),
        )

        for label, text in cases:
            with self.subTest(label=label):
                with self.assertRaises(plans.PlanError):
                    plans.parse_readiness_metadata(text)

    def test_write_readiness_metadata_inserts_after_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Split from**: `/source.md`",
                        "**Sessions**: 1",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            metadata = plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="deadbeef",
                checked_at="2026-06-19T00:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, metadata)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertIn("**Readiness**: loop-ready", text)
            self.assertIn("**Readiness SHA-256**: `deadbeef`", text)
            self.assertIn("**Readiness checked at**: 2026-06-19T00:00:00Z", text)

            sessions_index = text.index("## Sessions")
            readiness_index = text.index("**Readiness**:")
            sessions_count_index = text.index("**Sessions**: 1")
            self.assertLess(sessions_count_index, readiness_index)
            self.assertLess(readiness_index, sessions_index)

            roundtrip = plans.parse_readiness_metadata(text)
            self.assertEqual(metadata, roundtrip)

            unchanged = plans.write_readiness_metadata(plan_dir, metadata)
            self.assertFalse(unchanged)

    def test_write_readiness_metadata_replaces_existing_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 1",
                        "**Readiness**: draft",
                        "**Readiness SHA-256**: `oldhash`",
                        "**Readiness checked at**: 2026-01-01T00:00:00Z",
                        "",
                        "## Sessions",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            updated = plans.ReadinessMetadata(
                state="reviewed",
                readiness_sha256="newhash",
                checked_at="2026-06-19T12:00:00Z",
            )
            changed = plans.write_readiness_metadata(plan_dir, updated)

            self.assertTrue(changed)
            text = manifest_path.read_text(encoding="utf-8")
            self.assertEqual(1, text.count("**Readiness**:"))
            self.assertEqual(1, text.count("**Readiness SHA-256**:"))
            self.assertEqual(1, text.count("**Readiness checked at**:"))
            self.assertIn("**Readiness**: reviewed", text)
            self.assertIn("`newhash`", text)
            self.assertNotIn("oldhash", text)
            self.assertNotIn("2026-01-01", text)
            self.assertEqual(updated, plans.parse_readiness_metadata(text))

    def test_stable_readiness_sha256_ignores_handoff_status_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))

            baseline = plans.stable_readiness_sha256(plan_dir)

            (plan_dir / "session-1.md").write_text(
                "\n".join(
                    [
                        "# Session 1: Test",
                        "",
                        "Instructions.",
                        "",
                        "## Handoff",
                        "",
                        "- **Status**: complete",
                        "- **Files modified**: src/foo.py",
                        "- **Key decisions**: chose option A",
                        "- **Blockers for next session**: (none)",
                        "- **Notes**: tests green",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_status_table_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending", "pending"))
            manifest_path = plan_dir / "manifest.md"
            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | pending |",
                        "| 2 | Two | pending |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            baseline = plans.stable_readiness_sha256(plan_dir)

            manifest_path.write_text(
                "\n".join(
                    [
                        "# Plan",
                        "",
                        "**Sessions**: 2",
                        "",
                        "## Sessions",
                        "",
                        "| # | Title | Status |",
                        "|---|---|---|",
                        "| 1 | One | complete |",
                        "| 2 | Two | complete |",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_ignores_readiness_metadata_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=baseline,
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="reviewed",
                    readiness_sha256="differenthash",
                    checked_at="2027-01-01T00:00:00Z",
                ),
            )
            self.assertEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_changes_when_instructions_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text.replace("Instructions.", "Instructions changed."),
                encoding="utf-8",
            )

            self.assertNotEqual(baseline, plans.stable_readiness_sha256(plan_dir))

    def test_stable_readiness_sha256_tracks_expansion_and_validation_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            baseline = plans.stable_readiness_sha256(plan_dir)

            expansion = plan_dir / "session-1.expand.md"
            expansion.write_text("notes\n", encoding="utf-8")
            with_expansion = plans.stable_readiness_sha256(plan_dir)
            self.assertNotEqual(baseline, with_expansion)

            validation = plan_dir / "parallel-validation.md"
            validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Safe concurrent sessions**: none",
                        "",
                        "## Plan Readiness",
                        "",
                        "- **Recorded state**: draft",
                        "- **Recorded SHA-256**: old",
                        "- **Recomputed SHA-256**: new",
                        "- **Divergence**: recorded SHA differs from recomputed SHA",
                        "",
                        "## Preflight Snapshot",
                        "",
                        "- **Findings**: 1R / 1Y / 0G",
                        "- **Reason**: preflight-red",
                        "",
                        "## Session-Run Precedence",
                        "",
                        "Dependency and ownership claims remain authoritative.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with_validation = plans.stable_readiness_sha256(plan_dir)
            self.assertNotEqual(with_expansion, with_validation)

            validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Safe concurrent sessions**: none",
                        "",
                        "## Plan Readiness",
                        "",
                        "- **Recorded state**: loop-ready",
                        "- **Recorded SHA-256**: current",
                        "- **Recomputed SHA-256**: current",
                        "- **Divergence**: none",
                        "",
                        "## Preflight Snapshot",
                        "",
                        "- **Findings**: 0R / 0Y / 2G",
                        "- **Reason**: loop-ready-marked",
                        "",
                        "## Session-Run Precedence",
                        "",
                        "Dependency and ownership claims remain authoritative.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                with_validation, plans.stable_readiness_sha256(plan_dir)
            )

            validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Safe concurrent sessions**: 1, 2",
                        "",
                        "## Plan Readiness",
                        "",
                        "- **Recorded state**: loop-ready",
                        "- **Recorded SHA-256**: current",
                        "- **Recomputed SHA-256**: current",
                        "- **Divergence**: none",
                        "",
                        "## Preflight Snapshot",
                        "",
                        "- **Findings**: 0R / 0Y / 2G",
                        "- **Reason**: loop-ready-marked",
                        "",
                        "## Session-Run Precedence",
                        "",
                        "Dependency and ownership claims remain authoritative.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self.assertNotEqual(
                with_validation, plans.stable_readiness_sha256(plan_dir)
            )

    def test_parse_completion_contract_returns_none_when_section_absent(self) -> None:
        text = "\n".join(
            [
                "# Session 1: No contract",
                "",
                "Instructions.",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        self.assertIsNone(contracts.parse_completion_contract(text, session_id="1"))

    def test_parse_completion_contract_parses_done_when_bullets_and_recipe(
        self,
    ) -> None:
        text = "\n".join(
            [
                "# Session 2: Contract Parser",
                "",
                "Instructions.",
                "",
                "## Completion Contract",
                "",
                "### Done when",
                "- contracts.py exists",
                "- parser returns CompletionContract",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "session": "2",',
                '  "checks": [',
                "    {",
                '      "name": "module-exists",',
                '      "type": "file_exists",',
                '      "path": "agent_session_loop/contracts.py"',
                "    },",
                "    {",
                '      "type": "command",',
                '      "argv": ["python3", "-m", "unittest", "tests.t"],',
                '      "timeout_seconds": 30',
                "    },",
                "    {",
                '      "type": "regex_in_file",',
                '      "path": "agent_session_loop/contracts.py",',
                r'      "pattern": "^def parse_completion_contract"',
                "    }",
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id=2)

        self.assertIsNotNone(contract)
        self.assertEqual("2", contract.session_id.value)
        self.assertEqual(
            ("contracts.py exists", "parser returns CompletionContract"),
            contract.done_when,
        )
        kinds = [check.kind for check in contract.verify.checks]
        self.assertEqual(["file_exists", "command", "regex_in_file"], kinds)
        file_check, command_check, regex_check = contract.verify.checks
        self.assertEqual("module-exists", file_check.name)
        self.assertEqual("agent_session_loop/contracts.py", file_check.path)
        self.assertEqual("check-2", command_check.name)
        self.assertEqual(
            ("python3", "-m", "unittest", "tests.t"), command_check.argv
        )
        self.assertEqual(30.0, command_check.timeout_seconds)
        self.assertIsNone(command_check.cwd)
        self.assertEqual("check-3", regex_check.name)
        self.assertEqual("^def parse_completion_contract", regex_check.pattern)

    def test_parse_completion_contract_requires_done_when_bullets(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "Done when"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_requires_json_fenced_block(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "We forgot to add a fenced JSON block.",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "fenced code block"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_non_json_fence_language(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```yaml",
                "checks: []",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "json"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_session_id_field(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "session": "3",',
                '  "checks": [{"type": "file_exists", "path": "a"}]',
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "expected '2'"):
            contracts.parse_completion_contract(text, session_id="2")

    def test_parse_completion_contract_requires_non_empty_checks_list(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": []}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "non-empty list"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_unknown_check_type(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "shell", "argv": ["echo", "hi"]}]}',
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "'type' must be one of"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_command_argv_and_timeout(
        self,
    ) -> None:
        cases = (
            (
                "empty argv",
                '{"checks": [{"type": "command", "argv": []}]}',
                "non-empty list",
            ),
            (
                "non-string argv element",
                '{"checks": [{"type": "command", "argv": ["py", 3]}]}',
                "non-empty string",
            ),
            (
                "zero timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 0}]}',
                "positive number",
            ),
            (
                "negative timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": -1}]}',
                "positive number",
            ),
            (
                "timeout too large",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": 99999}]}',
                "<=",
            ),
            (
                "non-numeric timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": "fast"}]}',
                "positive number",
            ),
            (
                "boolean timeout",
                '{"checks": [{"type": "command", "argv": ["x"], '
                '"timeout_seconds": true}]}',
                "positive number",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_command_check_defaults_and_cwd(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "checks": [{',
                '    "type": "command",',
                '    "argv": ["python3", "-V"],',
                '    "cwd": "subdir"',
                "  }]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")

        check = contract.verify.checks[0]
        self.assertEqual(contracts.DEFAULT_COMMAND_TIMEOUT_SECONDS, check.timeout_seconds)
        self.assertEqual("subdir", check.cwd)

    def test_parse_completion_contract_validates_file_exists_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "file_exists"}]}',
                "non-empty string",
            ),
            (
                "non-string path",
                '{"checks": [{"type": "file_exists", "path": 42}]}',
                "non-empty string",
            ),
            (
                "empty path",
                '{"checks": [{"type": "file_exists", "path": ""}]}',
                "non-empty string",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_validates_regex_in_file_check(self) -> None:
        cases = (
            (
                "missing path",
                '{"checks": [{"type": "regex_in_file", "pattern": "x"}]}',
                "'path'",
            ),
            (
                "missing pattern",
                '{"checks": [{"type": "regex_in_file", "path": "a"}]}',
                "'pattern'",
            ),
            (
                "invalid regex",
                '{"checks": [{"type": "regex_in_file", "path": "a", '
                '"pattern": "("}]}',
                "not a valid regex",
            ),
        )

        for label, json_block, pattern in cases:
            with self.subTest(label=label):
                text = "\n".join(
                    [
                        "## Completion Contract",
                        "",
                        "### Done when",
                        "- something",
                        "",
                        "### Verify",
                        "",
                        "```json",
                        json_block,
                        "```",
                        "",
                        "## Handoff",
                        "- **Status**: pending",
                    ]
                )
                with self.assertRaisesRegex(plans.PlanError, pattern):
                    contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_rejects_duplicate_check_names(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{",
                '  "checks": [',
                '    {"name": "same", "type": "file_exists", "path": "a"},',
                '    {"name": "same", "type": "file_exists", "path": "b"}',
                "  ]",
                "}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "duplicated"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_reports_malformed_json(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                "{not valid json}",
                "```",
                "",
                "## Handoff",
                "- **Status**: pending",
            ]
        )

        with self.assertRaisesRegex(plans.PlanError, "Verify JSON is invalid"):
            contracts.parse_completion_contract(text, session_id="1")

    def test_parse_completion_contract_ignores_handoff_section_content(self) -> None:
        text = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- something",
                "",
                "### Verify",
                "",
                "```json",
                '{"checks": [{"type": "file_exists", "path": "a"}]}',
                "```",
                "",
                "## Handoff",
                "",
                "- **Status**: pending",
                "- **Notes**: some prose that mentions ### Verify and ### Done when",
                "",
            ]
        )

        contract = contracts.parse_completion_contract(text, session_id="1")
        self.assertEqual(("something",), contract.done_when)
        self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_loads_from_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract_block = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- tests pass",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{",
                    '  "checks": [',
                    '    {"type": "file_exists", "path": "agent_session_loop/contracts.py"}',
                    "  ]",
                    "}",
                    "```",
                ]
            )
            write_plan(
                plan_dir,
                ("pending",),
                contracts_by_id={"1": contract_block},
            )

            contract = contracts.read_completion_contract(plan_dir, "1")

            self.assertIsNotNone(contract)
            self.assertEqual("1", contract.session_id.value)
            self.assertEqual(("tests pass",), contract.done_when)
            self.assertEqual(1, len(contract.verify.checks))

    def test_read_completion_contract_raises_for_missing_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            with self.assertRaisesRegex(plans.PlanError, "missing session file"):
                contracts.read_completion_contract(plan_dir, "9")

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


class VerificationExecutorTests(unittest.TestCase):
    def _session_id(self, value: str = "1") -> plans.SessionId:
        return plans.parse_session_id(value)

    def _contract(
        self, checks: tuple[contracts.VerificationCheck, ...]
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=self._session_id(),
            done_when=("done",),
            verify=contracts.VerifyRecipe(checks=checks),
        )

    def test_command_check_passes_when_argv_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="echo",
                argv=("python3", "-c", "import sys; sys.exit(0)"),
                timeout_seconds=10.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual("echo", result.name)
            self.assertEqual("command", result.kind)
            self.assertEqual(0, result.returncode)
            self.assertIsNone(result.failure_reason)

    def test_command_check_fails_on_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="exit-1",
                argv=("python3", "-c", "import sys; sys.exit(7)"),
                timeout_seconds=10.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-failed", result.failure_reason)
            self.assertEqual(7, result.returncode)
            self.assertIn("exited 7", result.detail)

    def test_command_check_fails_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="sleeper",
                argv=("python3", "-c", "import time; time.sleep(5)"),
                timeout_seconds=0.1,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-timeout", result.failure_reason)
            self.assertIsNone(result.returncode)
            self.assertIn("timed out", result.detail)

    def test_command_check_reports_command_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.CommandCheck(
                name="missing",
                argv=("definitely-not-a-real-binary-xyz",),
                timeout_seconds=5.0,
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("command-not-found", result.failure_reason)
            self.assertIn("definitely-not-a-real-binary-xyz", result.detail)

    def test_command_check_resolves_cwd_under_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            sub = repo_root / "nested"
            sub.mkdir()
            check = contracts.CommandCheck(
                name="pwd",
                argv=(
                    "python3",
                    "-c",
                    "import os, sys; sys.stdout.write(os.getcwd())",
                ),
                timeout_seconds=10.0,
                cwd="nested",
            )
            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertEqual(
                str(plans.canonicalize_path(sub)),
                str(plans.canonicalize_path(result.stdout)),
            )

    def test_file_exists_check_passes_when_file_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "target.txt").write_text("data\n", encoding="utf-8")
            check = contracts.FileExistsCheck(name="target", path="target.txt")

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)
            self.assertIsNone(result.failure_reason)

    def test_file_exists_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.FileExistsCheck(name="absent", path="nope.txt")

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)
            self.assertIn("nope.txt", result.detail)

    def test_regex_in_file_check_passes_when_pattern_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text(
                "def parse_completion_contract():\n    pass\n",
                encoding="utf-8",
            )
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("pass", result.status)

    def test_regex_in_file_check_fails_when_pattern_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "module.py").write_text("nothing\n", encoding="utf-8")
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="module.py",
                pattern=r"^def parse_completion_contract",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("regex-not-matched", result.failure_reason)
            self.assertIn("module.py", result.detail)

    def test_regex_in_file_check_fails_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            check = contracts.RegexInFileCheck(
                name="symbol",
                path="missing.py",
                pattern=r"anything",
            )

            result = verification.run_check(check, repo_root=repo_root)

            self.assertEqual("fail", result.status)
            self.assertEqual("file-missing", result.failure_reason)

    def test_run_verification_aggregates_all_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.FileExistsCheck(name="absent", path="absent.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertEqual("1", result.session_id.value)
            self.assertEqual(3, len(result.checks))
            self.assertFalse(result.passed)
            failure_names = {failure.name for failure in result.failures}
            self.assertEqual({"absent"}, failure_names)

    def test_run_verification_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "present.txt").write_text("hi\n", encoding="utf-8")
            contract = self._contract(
                checks=(
                    contracts.FileExistsCheck(name="present", path="present.txt"),
                    contracts.CommandCheck(
                        name="ok",
                        argv=("python3", "-c", "import sys; sys.exit(0)"),
                        timeout_seconds=10.0,
                    ),
                )
            )

            result = verification.run_verification(contract, repo_root=repo_root)

            self.assertTrue(result.passed)
            self.assertEqual((), result.failures)


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
        self.assertIn(
            runner.RUNNER_MANAGED_SESSION_ARG,
            runner.build_session_prompt("codex", "/tmp/plan"),
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
            "harness": "codex",
            "reason": "all-complete",
            "run_started_at": "2026-06-11T12:00:00Z",
            "run_finished_at": "2026-06-11T12:00:03Z",
            "state_path": str(tmp_dir / "state.json"),
        }

    def test_loop_payload_uses_active_turn_harness_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            active_turn = {
                "harness": "claude",
                "turn_index": 2,
                "session": "2.1",
                "session_id": "2.1",
                "total_sessions": 4,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
            }

            hook_payload = runner._agent_mux_loop_payload(
                payload,
                reason=runner.AGENT_MUX_TURN_START_REASON,
                active_turn=active_turn,
            )

            self.assertEqual("agent_session_loop", hook_payload["source"])
            self.assertEqual("turn_started", hook_payload["reason"])
            self.assertEqual("claude", hook_payload["harness"])
            self.assertEqual(payload["state_path"], hook_payload["state_path"])
            self.assertEqual(2, hook_payload["turn_index"])
            self.assertEqual("2.1", hook_payload["session"])
            self.assertEqual("2.1", hook_payload["session_id"])
            self.assertEqual(4, hook_payload["total_sessions"])

    def test_start_and_turn_notifications_build_agent_mux_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            payload = self.finish_payload(tmp_dir)
            payload["run_finished_at"] = None
            active_turn = {
                "harness": "claude",
                "turn_index": 1,
                "session": 1,
                "session_id": "1",
                "total_sessions": 2,
                "artifact_dir": str(tmp_dir / "turn"),
                "stdout_path": str(tmp_dir / "stdout.log"),
                "stderr_path": str(tmp_dir / "stderr.log"),
                "last_message_path": str(tmp_dir / "last-message.txt"),
                "result_path": str(tmp_dir / "result.json"),
                "started_at": "2026-06-11T12:01:00Z",
            }
            completed = subprocess.CompletedProcess(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopStarted"],
                0,
                stdout="",
                stderr="",
            )

            with (
                mock.patch.object(runner.shutil, "which", return_value="/opt/amx"),
                mock.patch.object(
                    runner.subprocess,
                    "run",
                    return_value=completed,
                ) as run,
            ):
                runner._notify_agent_mux_loop_started(payload)
                runner._notify_agent_mux_loop_turn_started(payload, active_turn)

            self.assertEqual(2, run.call_count)
            start_args, start_kwargs = run.call_args_list[0]
            turn_args, turn_kwargs = run.call_args_list[1]
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopStarted"],
                start_args[0],
            )
            self.assertEqual(
                ["/opt/amx", "hook-indicator", "AgentSessionLoopTurnStarted"],
                turn_args[0],
            )
            self.assertEqual(
                runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                start_kwargs["timeout"],
            )
            self.assertEqual(
                runner.AGENT_MUX_NOTIFY_TIMEOUT_SECONDS,
                turn_kwargs["timeout"],
            )
            start_payload = json.loads(start_kwargs["input"])
            turn_payload = json.loads(turn_kwargs["input"])
            self.assertEqual("run_started", start_payload["reason"])
            self.assertEqual("codex", start_payload["harness"])
            self.assertEqual(payload["state_path"], start_payload["state_path"])
            self.assertEqual("turn_started", turn_payload["reason"])
            self.assertEqual("claude", turn_payload["harness"])
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])

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
                    "state_path": payload["state_path"],
                    "harness": "codex",
                    "run_started_at": "2026-06-11T12:00:00Z",
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
            allow_not_loop_ready=True,
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
            "--allow-not-loop-ready",
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

    def test_loop_lifecycle_notifications_run_after_state_writes(self) -> None:
        listener = FakeRunControlListener()
        observed: list[tuple[str, object]] = []

        def read_state_payload(payload: dict[str, object]) -> dict[str, object]:
            return json.loads(Path(str(payload["state_path"])).read_text(encoding="utf-8"))

        def notify_started(payload: dict[str, object]) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("started", state_payload["active_turn"]))
            self.assertEqual(payload["run_id"], state_payload["run_id"])
            self.assertEqual("codex", state_payload["harness"])
            self.assertIsNone(state_payload["active_turn"])

        def notify_turn_started(
            payload: dict[str, object],
            active_turn: dict[str, object],
        ) -> None:
            state_payload = read_state_payload(payload)
            state_turn = state_payload["active_turn"]
            self.assertIsInstance(state_turn, dict)
            assert isinstance(state_turn, dict)
            observed.append(("turn_started", state_turn["harness"]))
            self.assertEqual(active_turn["turn_index"], state_turn["turn_index"])
            self.assertEqual("codex", state_turn["harness"])

        def notify_finished(payload: dict[str, object]) -> None:
            state_payload = read_state_payload(payload)
            observed.append(("finished", state_payload["run_finished_at"]))
            self.assertEqual(payload["run_finished_at"], state_payload["run_finished_at"])
            self.assertIsNone(state_payload["active_turn"])

        with (
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_started",
                side_effect=notify_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _plan_dir, _state_dir, _fake_codex = (
                self.run_locked_with_listener(
                    mode="success",
                    statuses=("pending",),
                    listener=listener,
                )
            )

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            ["started", "turn_started", "finished"],
            [name for name, _value in observed],
        )

    def test_copilot_run_from_other_cwd_notifies_agent_mux_with_project_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            state_dir = tmp_dir / "state"
            config_home = tmp_dir / "config"
            config_path = config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
            config_path.parent.mkdir(parents=True, exist_ok=True)
            fake_codex = install_fake_codex(tmp_dir)
            fake_copilot = install_fake_harness(tmp_dir, "copilot")
            _fake_amx, amx_log = install_fake_amx(tmp_dir)
            caller_dir = tmp_dir / "caller"
            project_dir = tmp_dir / "project"
            caller_dir.mkdir()
            project_dir.mkdir()
            write_plan(plan_dir, ("pending",))
            config_path.write_text(
                json.dumps(
                    {
                        "codex": {"executable": str(fake_codex)},
                        "copilot": {"executable": str(fake_copilot)},
                    }
                ),
                encoding="utf-8",
            )

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "FAKE_CODEX_MODE": "success",
                    "FAKE_CODEX_SLEEP": "5",
                    "FAKE_AMX_LOG": str(amx_log),
                    "PATH": str(tmp_dir)
                    + os.pathsep
                    + os.environ.get("PATH", ""),
                    "PYTHONPATH": str(REPO_ROOT)
                    + os.pathsep
                    + env.get("PYTHONPATH", ""),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(project_dir),
                    "--harness",
                    "copilot",
                    "--state-dir",
                    str(state_dir),
                    "--json",
                    "--allow-not-loop-ready",
                ],
                cwd=caller_dir,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            payload = json.loads(completed.stdout)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(payload["ok"])
            self.assertEqual("copilot", payload["harness"])
            expected_cwd = str(plans.canonicalize_path(project_dir))
            self.assertEqual(expected_cwd, payload["cwd"])

            amx_records = read_jsonl(amx_log)
            self.assertEqual(
                [
                    runner.AGENT_MUX_START_EVENT,
                    runner.AGENT_MUX_TURN_START_EVENT,
                    runner.AGENT_MUX_FINISH_EVENT,
                ],
                [record["argv"][-1] for record in amx_records],
            )
            self.assertEqual(
                {str(plans.canonicalize_path(caller_dir))},
                {record["cwd"] for record in amx_records},
            )

            hook_payloads = [
                json.loads(str(record["stdin"])) for record in amx_records
            ]
            self.assertEqual(
                [
                    runner.AGENT_MUX_START_REASON,
                    runner.AGENT_MUX_TURN_START_REASON,
                    runner.AGENT_MUX_FINISH_REASON,
                ],
                [hook_payload["reason"] for hook_payload in hook_payloads],
            )
            for hook_payload in hook_payloads:
                self.assertEqual("agent_session_loop", hook_payload["source"])
                self.assertEqual("copilot", hook_payload["harness"])
                self.assertEqual(expected_cwd, hook_payload["cwd"])
                self.assertEqual(payload["plan_dir"], hook_payload["plan_dir"])
                self.assertEqual(payload["state_path"], hook_payload["state_path"])
                self.assertEqual(payload["run_id"], hook_payload["run_id"])
                self.assertTrue(Path(str(hook_payload["state_path"])).exists())

            turn_payload = hook_payloads[1]
            self.assertEqual(1, turn_payload["turn_index"])
            self.assertEqual(1, turn_payload["session"])
            self.assertEqual("1", turn_payload["session_id"])
            self.assertEqual(
                str(payload["turns"][0]["artifact_dir"]),
                turn_payload["artifact_dir"],
            )
            self.assertIsNone(
                json.loads(Path(payload["state_path"]).read_text(encoding="utf-8"))[
                    "active_turn"
                ]
            )

    def test_harness_switch_updates_agent_mux_turn_notification_harness(self) -> None:
        hook_payloads: list[dict[str, object]] = []
        finish_payloads: list[dict[str, object]] = []

        def notify_turn_started(
            payload: dict[str, object],
            active_turn: dict[str, object],
        ) -> None:
            hook_payloads.append(
                runner._agent_mux_loop_payload(
                    payload,
                    reason=runner.AGENT_MUX_TURN_START_REASON,
                    active_turn=active_turn,
                )
            )

        def notify_finished(payload: dict[str, object]) -> None:
            finish_payloads.append(
                runner._agent_mux_loop_payload(
                    payload,
                    reason=runner.AGENT_MUX_FINISH_REASON,
                )
            )

        with (
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_turn_started",
                side_effect=notify_turn_started,
            ),
            mock.patch.object(
                runner,
                "_notify_agent_mux_loop_finished",
                side_effect=notify_finished,
            ),
        ):
            exit_code, payload, _calls, _plan_dir, _state_dir = (
                self.run_with_mocked_harness_switch()
            )

        self.assertEqual(0, exit_code)
        self.assertEqual(
            ["codex", "claude"],
            [turn["harness"] for turn in payload["turns"]],
        )
        self.assertEqual(
            ["codex", "claude"],
            [hook_payload["harness"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["turn_index"] for hook_payload in hook_payloads],
        )
        self.assertEqual(
            [1, 2],
            [hook_payload["session"] for hook_payload in hook_payloads],
        )
        self.assertEqual(1, len(finish_payloads))
        self.assertEqual("claude", finish_payloads[0]["harness"])

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
            "--allow-not-loop-ready",
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
                "harness": "codex",
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
            heartbeat_payloads = [
                record["payload"]
                for record in event_records
                if record["name"] == events.TURN_HEARTBEAT_EVENT
            ]
            self.assertEqual({"codex"}, {payload["harness"] for payload in heartbeat_payloads})
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

    def test_live_turn_action_required_payload_includes_harness(self) -> None:
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
                "harness": "codex",
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
            live_turn = runner._LiveTurn(
                payload=payload,
                event_path=event_path,
                progress=runner._ProgressReporter(enabled=False),
                active_turn=active_turn,
                heartbeat_interval=60,
            )

            live_turn.start()
            live_turn.record_activity("stderr", "approval required before continuing")
            live_turn.stop()
            live_turn.finish(runner._utc_now())

            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            event_records = read_jsonl(event_path)
            action_events = [
                record["payload"]
                for record in event_records
                if record["name"] == events.ACTION_REQUIRED_EVENT
            ]
            self.assertEqual(1, len(action_events))
            self.assertEqual("codex", action_events[0]["harness"])
            self.assertEqual("codex", state_payload["action_required"]["harness"])

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

    def test_structured_runner_pause_signal_uses_embedded_reason(self) -> None:
        completed, payload, _plan_dir, _state_dir, _fake_codex = self.run_loop(
            mode="runner-pause-unsafe",
            statuses=("pending",),
        )

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("unsafe", payload["pause_reason"])
        self.assertEqual("unsafe", payload["reason"])
        self.assertIn("plan readiness is not current", payload["pause_message"])
        self.assertIn(payload["pause_reason"], events.PAUSE_REASONS)
        self.assertEqual("unsafe", payload["turns"][0]["status_result"])

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


def _contract_block(check_payload: str, *, session: str = "1") -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- recipe runs",
            "",
            "### Verify",
            "",
            "```json",
            f'{{"session": "{session}", "checks": [{check_payload}]}}',
            "```",
        ]
    )


class VerifyCliTests(unittest.TestCase):
    def _run_verify(
        self,
        plan_dir: Path,
        *,
        session: str,
        cwd: Path,
        json_output: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(plan_dir),
            "--session",
            session,
            "--cwd",
            str(cwd),
        ]
        if json_output:
            argv.append("--json")
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_verify_json_passes_when_all_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "target.txt").write_text("ok\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "target.txt", "name": "target"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["passed"])
            self.assertEqual("verify", payload["command"])
            self.assertEqual("ok", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual(
                str(plans.canonicalize_path(repo_root)), payload["cwd"]
            )
            self.assertEqual(str(plan_dir), payload["plan_dir"])
            self.assertEqual(1, payload["recipe"]["check_count"])
            self.assertEqual(["file_exists"], payload["recipe"]["kinds"])
            self.assertEqual(["target"], payload["recipe"]["names"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("target", check["name"])
            self.assertEqual("file_exists", check["kind"])
            self.assertEqual("pass", check["status"])
            self.assertIsNone(check["failure_reason"])

    def test_verify_json_failure_reports_check_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertFalse(payload["passed"])
            self.assertFalse(payload["paused"])
            self.assertEqual("verify-failed", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("verification check", payload["error"])
            self.assertEqual(1, len(payload["checks"]))
            check = payload["checks"][0]
            self.assertEqual("absent", check["name"])
            self.assertEqual("fail", check["status"])
            self.assertEqual("file-missing", check["failure_reason"])
            self.assertIn("absent.txt", check["detail"])

    def test_verify_json_no_contract_returns_no_contract_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("no-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertEqual([], payload["checks"])
            self.assertEqual(0, payload["recipe"]["check_count"])
            self.assertFalse(payload["passed"])

    def test_verify_json_malformed_contract_reports_plan_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- something",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{not valid json",
                    "```",
                ]
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(plan_dir, session="1", cwd=repo_root)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual("malformed-contract", payload["reason"])
            self.assertEqual("1", payload["session"])
            self.assertIn("Verify JSON is invalid", payload["error"])

    def test_verify_json_invalid_session_id_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            write_plan(plan_dir, ("pending",))

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_session_loop",
                    "verify",
                    str(plan_dir),
                    "--session",
                    "not-a-session",
                    "--cwd",
                    str(repo_root),
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
            self.assertEqual("invalid-session", payload["reason"])
            self.assertEqual("not-a-session", payload["session"])

    def test_verify_text_output_shows_per_check_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            (repo_root / "present.txt").write_text("yes\n", encoding="utf-8")
            contract = _contract_block(
                '{"type": "file_exists", "path": "present.txt", "name": "present"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Session: 1", completed.stdout)
            self.assertIn("[PASS] file_exists present", completed.stdout)
            self.assertIn("Verify: passed", completed.stdout)

    def test_verify_text_output_reports_failure_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            plan_dir = tmp_dir / "plan"
            repo_root = tmp_dir / "repo"
            repo_root.mkdir()
            contract = _contract_block(
                '{"type": "file_exists", "path": "absent.txt", "name": "absent"}'
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )

            completed = self._run_verify(
                plan_dir, session="1", cwd=repo_root, json_output=False
            )

            self.assertEqual(1, completed.returncode)
            self.assertIn("[FAIL] file_exists absent", completed.stdout)
            self.assertIn("Verify: failed", completed.stderr)


def _write_loop_ready_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
    state: plans.ReadinessState = "loop-ready",
) -> plans.ReadinessMetadata:
    """Write a plan with valid contracts and a synced readiness block."""
    contract_blocks: dict[str, str] = {}
    for number in range(1, len(statuses) + 1):
        key = str(number)
        contract_blocks[key] = (
            contracts_by_id or {}
        ).get(
            key,
            _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{number}"}}',
                session=key,
            ),
        )
    write_plan(plan_dir, statuses, contracts_by_id=contract_blocks)
    expected = plans.stable_readiness_sha256(plan_dir)
    metadata = plans.ReadinessMetadata(
        state=state,
        readiness_sha256=expected,
        checked_at="2026-06-19T00:00:00Z",
    )
    plans.write_readiness_metadata(plan_dir, metadata)
    return metadata


class PreflightCoreTests(unittest.TestCase):
    def test_missing_plan_directory_emits_red_plan_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "does-not-exist"

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            self.assertIsNone(report.snapshot)
            self.assertEqual(1, len(report.findings))
            finding = report.findings[0]
            self.assertEqual("red", finding.severity)
            self.assertEqual("plan-structure", finding.category)
            self.assertIn("does not exist", finding.message)

    def test_missing_context_or_manifest_emits_red_plan_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            (plan_dir / "context.md").unlink()

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            categories = {finding.category for finding in report.findings}
            self.assertIn("plan-structure", categories)
            structure = [
                finding
                for finding in report.findings
                if finding.category == "plan-structure"
            ]
            self.assertTrue(structure)
            self.assertEqual("red", structure[0].severity)
            self.assertIn("context.md", structure[0].message)

    def test_non_canonical_session_filenames_emit_red_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            plan_dir.mkdir()
            (plan_dir / "context.md").write_text("# Context\n", encoding="utf-8")
            (plan_dir / "manifest.md").write_text("# Manifest\n", encoding="utf-8")
            (plan_dir / "session-01.md").write_text(
                "# Session 01\n\n## Handoff\n\n- **Status**: pending\n",
                encoding="utf-8",
            )

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            canonical_findings = [
                finding
                for finding in report.findings
                if finding.category == "canonical-session-id"
            ]
            self.assertEqual(1, len(canonical_findings))
            finding = canonical_findings[0]
            self.assertEqual("red", finding.severity)
            self.assertIn("session-01.md", finding.message)
            self.assertIn("session-1.md", finding.detail)

    def test_missing_readiness_metadata_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))

            report = preflight.run_core_preflight(plan_dir)

            self.assertFalse(report.ok)
            readiness_findings = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertEqual(1, len(readiness_findings))
            finding = readiness_findings[0]
            self.assertEqual("red", finding.severity)
            self.assertIn("missing readiness", finding.message)
            self.assertIsNone(report.readiness)
            self.assertIsNotNone(report.expected_readiness_sha256)

    def test_readiness_sha_mismatch_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            stored = plans.parse_readiness_metadata(
                (plan_dir / "manifest.md").read_text(encoding="utf-8")
            )
            assert stored is not None
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state=stored.state,
                    readiness_sha256="00" * 32,
                    checked_at=stored.checked_at,
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            mismatches = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertTrue(mismatches)
            self.assertEqual("red", mismatches[0].severity)
            self.assertIn("SHA-256", mismatches[0].message)
            self.assertIn(report.expected_readiness_sha256, mismatches[0].detail)

    def test_draft_readiness_state_is_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",), state="draft")

            report = preflight.run_core_preflight(plan_dir)

            yellow = [
                finding
                for finding in report.findings
                if finding.category == "manifest-readiness"
            ]
            self.assertEqual(1, len(yellow))
            self.assertEqual("yellow", yellow[0].severity)
            self.assertIn("draft", yellow[0].message)
            self.assertFalse(report.ok)

    def test_missing_completion_contract_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            missing = [
                finding
                for finding in report.findings
                if finding.category == "missing-contract"
            ]
            self.assertEqual(1, len(missing))
            self.assertEqual("red", missing[0].severity)
            self.assertEqual("1", missing[0].session_id)
            self.assertIn("Completion Contract", missing[0].message)
            self.assertEqual({}, report.contracts_by_session)

    def test_malformed_completion_contract_emits_red(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            broken_contract = "\n".join(
                [
                    "## Completion Contract",
                    "",
                    "### Done when",
                    "- something",
                    "",
                    "### Verify",
                    "",
                    "```json",
                    "{not valid json",
                    "```",
                ]
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": broken_contract}
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            malformed = [
                finding
                for finding in report.findings
                if finding.category == "malformed-contract"
            ]
            self.assertEqual(1, len(malformed))
            self.assertEqual("red", malformed[0].severity)
            self.assertEqual("1", malformed[0].session_id)
            self.assertIn("malformed", malformed[0].message)

    def test_valid_loop_ready_plan_is_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending", "pending"))

            report = preflight.run_core_preflight(plan_dir)

            self.assertTrue(
                report.ok,
                msg=[
                    (finding.category, finding.severity, finding.message)
                    for finding in report.findings
                ],
            )
            self.assertEqual((), report.findings)
            self.assertIsNotNone(report.snapshot)
            self.assertIsNotNone(report.readiness)
            self.assertEqual({"1", "2"}, set(report.contracts_by_session))

    def test_stale_parallel_validation_readiness_snapshot_does_not_block(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            validation = plan_dir / "parallel-validation.md"
            validation.write_text(
                "\n".join(
                    [
                        "# Parallel Validation",
                        "",
                        "## Summary",
                        "",
                        "- **Safe concurrent sessions**: none",
                        "",
                        "## Plan Readiness",
                        "",
                        "- **Recorded state**: draft",
                        "- **Recorded SHA-256**: old",
                        "- **Recomputed SHA-256**: new",
                        "- **Divergence**: recorded SHA differs from recomputed SHA",
                        "",
                        "## Preflight Snapshot",
                        "",
                        "- **Findings**: 1R / 1Y / 0G",
                        "- **Reason**: preflight-red",
                        "",
                        "## Session-Run Precedence",
                        "",
                        "Dependency and ownership claims remain authoritative.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            current_sha = plans.stable_readiness_sha256(plan_dir)
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=current_sha,
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            self.assertTrue(
                report.ok,
                msg=[
                    (finding.category, finding.severity, finding.message)
                    for finding in report.findings
                ],
            )
            self.assertEqual(current_sha, report.expected_readiness_sha256)

            validation.write_text(
                validation.read_text(encoding="utf-8").replace(
                    "- **Findings**: 1R / 1Y / 0G",
                    "- **Findings**: 0R / 0Y / 2G",
                ),
                encoding="utf-8",
            )

            refreshed_report = preflight.run_core_preflight(plan_dir)
            self.assertTrue(refreshed_report.ok)
            self.assertEqual(current_sha, refreshed_report.expected_readiness_sha256)

    def test_ambiguity_sentinel_emits_yellow_above_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            text = text.replace("Instructions.", "Instructions. TODO finalize.")
            session_path.write_text(text, encoding="utf-8")
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            sentinels = [
                finding
                for finding in report.findings
                if finding.category == "ambiguity-sentinel"
            ]
            self.assertEqual(1, len(sentinels))
            self.assertEqual("yellow", sentinels[0].severity)
            self.assertEqual("1", sentinels[0].session_id)
            self.assertIn("TODO", sentinels[0].message)
            self.assertIsNotNone(sentinels[0].line)
            self.assertFalse(report.ok)

    def test_ambiguity_sentinel_ignores_handoff_template_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))
            session_path = plan_dir / "session-1.md"
            text = session_path.read_text(encoding="utf-8")
            session_path.write_text(
                text + "\n- **Notes**: TODO follow up here\n",
                encoding="utf-8",
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            sentinels = [
                finding
                for finding in report.findings
                if finding.category == "ambiguity-sentinel"
            ]
            self.assertEqual([], sentinels)

    def test_path_reference_flags_absolute_paths_as_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract = _contract_block(
                '{"type": "file_exists", "path": "/etc/hosts", "name": "abs"}',
                session="1",
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            path_findings = [
                finding
                for finding in report.findings
                if finding.category == "path-reference"
            ]
            self.assertEqual(1, len(path_findings))
            self.assertEqual("yellow", path_findings[0].severity)
            self.assertIn("absolute", path_findings[0].message)
            self.assertEqual("1", path_findings[0].session_id)

    def test_path_reference_flags_parent_traversal_as_yellow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            contract = _contract_block(
                '{"type": "regex_in_file", "path": "../escape.txt", '
                '"pattern": "x", "name": "escape"}',
                session="1",
            )
            write_plan(
                plan_dir, ("pending",), contracts_by_id={"1": contract}
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            report = preflight.run_core_preflight(plan_dir)

            path_findings = [
                finding
                for finding in report.findings
                if finding.category == "path-reference"
            ]
            self.assertEqual(1, len(path_findings))
            self.assertEqual("yellow", path_findings[0].severity)
            self.assertIn("escapes", path_findings[0].message)

    def test_report_ok_only_when_no_red_or_yellow_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",))

            ok_report = preflight.run_core_preflight(plan_dir)
            self.assertTrue(ok_report.ok)

            session_path = plan_dir / "session-1.md"
            session_path.write_text(
                session_path.read_text(encoding="utf-8").replace(
                    "Instructions.", "Instructions. FIXME me later."
                ),
                encoding="utf-8",
            )
            plans.write_readiness_metadata(
                plan_dir,
                plans.ReadinessMetadata(
                    state="loop-ready",
                    readiness_sha256=plans.stable_readiness_sha256(plan_dir),
                    checked_at="2026-06-19T00:00:00Z",
                ),
            )

            yellow_report = preflight.run_core_preflight(plan_dir)
            self.assertFalse(yellow_report.ok)
            self.assertEqual((), yellow_report.red_findings)
            self.assertEqual(1, len(yellow_report.yellow_findings))


_FIX_CATEGORIES = {
    "plan-recreate",
    "filename-rename",
    "manifest-update",
    "plan-edit",
    "invocation-fix",
    "env-install",
    "env-state",
    "session-recipe-edit",
    "informational",
}


class PreflightRepairHintTests(unittest.TestCase):
    def test_every_preflight_category_has_a_default_hint(self) -> None:
        for category in preflight.PREFLIGHT_CATEGORIES:
            with self.subTest(category=category):
                hint = preflight.REPAIR_HINTS[category]
                self.assertEqual(category, hint.category)
                self.assertTrue(hint.check_id)
                self.assertTrue(hint.message)
                self.assertTrue(hint.question_template)
                self.assertIn(hint.fix_category, _FIX_CATEGORIES)

    def test_all_repair_hint_check_ids_are_unique(self) -> None:
        check_ids: list[str] = []
        for hint in preflight.REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        for hint in preflight._CATEGORY_HINT_OVERRIDES.values():
            check_ids.append(hint.check_id)
        for hint in preflight.RUNTIME_REPAIR_HINTS.values():
            check_ids.append(hint.check_id)
        self.assertEqual(
            len(check_ids),
            len(set(check_ids)),
            msg=f"duplicate check_ids in repair-hint tables: {check_ids}",
        )

    def test_missing_contract_finding_resolves_to_add_hint(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="missing-contract",
            message="session 1 is missing a ## Completion Contract section",
            session_id="1",
            path="session-1.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("missing-contract.add", hint.check_id)
        self.assertEqual("missing-contract", hint.category)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_manifest_readiness_missing_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest is missing readiness metadata",
            path="manifest.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.missing", hint.check_id)
        self.assertEqual("manifest-update", hint.fix_category)

    def test_manifest_readiness_sha_mismatch_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="manifest-readiness",
            message="manifest readiness SHA-256 does not match current plan content",
            path="manifest.md",
            detail="recorded 00; expected aa",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("manifest-readiness.sha-mismatch", hint.check_id)

    def test_manifest_readiness_state_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="manifest-readiness",
            message="plan readiness state is 'draft'",
            path="manifest.md",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual(
            "manifest-readiness.state-not-loop-ready", hint.check_id
        )
        self.assertEqual("yellow", hint.severity_hint)

    def test_env_git_outside_worktree_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="env-git",
            message="cwd is not inside a git repository: /tmp/x",
            path="/tmp/x",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-git.outside-worktree", hint.check_id)
        self.assertEqual("invocation-fix", hint.fix_category)

    def test_env_git_dirty_worktree_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-git",
            message="git worktree is dirty; commit-mode runs require a clean tree",
            path="/tmp/repo",
            detail="commit-mode: per-session",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-git.dirty-worktree", hint.check_id)
        self.assertEqual("env-state", hint.fix_category)

    def test_env_lock_stale_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-lock",
            message="stale lock present at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.stale", hint.check_id)

    def test_env_lock_active_resolves_to_override(self) -> None:
        finding = preflight.PreflightFinding(
            severity="red",
            category="env-lock",
            message="active lock held by pid 1234 at /tmp/lock",
            path="/tmp/lock",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-lock.active", hint.check_id)

    def test_env_amx_green_finding_resolves_to_optional(self) -> None:
        finding = preflight.PreflightFinding(
            severity="green",
            category="env-amx",
            message="amx hook indicator not on PATH",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-amx.optional", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_env_turn_budget_yellow_resolves_to_split(self) -> None:
        finding = preflight.PreflightFinding(
            severity="yellow",
            category="env-turn-budget",
            message="30 pending sessions exceeds yellow threshold 25",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-turn-budget.split", hint.check_id)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_env_turn_budget_green_resolves_to_healthy(self) -> None:
        finding = preflight.PreflightFinding(
            severity="green",
            category="env-turn-budget",
            message="5 session(s) pending; rough turn budget ~15 turns",
        )
        hint = preflight.repair_hint_for_finding(finding)
        self.assertEqual("env-turn-budget.healthy", hint.check_id)
        self.assertEqual("informational", hint.fix_category)

    def test_report_repair_hints_align_with_findings_one_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            _write_loop_ready_plan(plan_dir, ("pending",), state="draft")

            report = preflight.run_core_preflight(plan_dir)

            self.assertEqual(len(report.findings), len(report.repair_hints))
            for finding, hint in zip(report.findings, report.repair_hints):
                self.assertEqual(finding.category, hint.category)

    def test_runtime_no_contract_resolves(self) -> None:
        hint = preflight.runtime_repair_hint("no-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.no-contract", hint.check_id)
        self.assertEqual("plan-edit", hint.fix_category)

    def test_runtime_malformed_contract_resolves(self) -> None:
        hint = preflight.runtime_repair_hint("malformed-contract")
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual("runtime.malformed-contract", hint.check_id)

    def test_runtime_verify_failed_without_failure_reason_returns_none(
        self,
    ) -> None:
        self.assertIsNone(preflight.runtime_repair_hint("verify-failed"))

    def test_runtime_verify_failed_command_not_found_resolves(self) -> None:
        hint = preflight.runtime_repair_hint(
            "verify-failed", failure_reason="command-not-found"
        )
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual(
            "runtime.verify-failed.command-not-found", hint.check_id
        )
        self.assertEqual("env-install", hint.fix_category)

    def test_runtime_verify_failed_every_failure_reason_resolves(self) -> None:
        # Every failure_reason emitted by verification.py must resolve so the
        # guided skill can surface a fix for any per-check failure.
        for failure_reason in (
            "command-failed",
            "command-timeout",
            "command-not-found",
            "command-error",
            "file-missing",
            "file-error",
            "regex-not-matched",
        ):
            with self.subTest(failure_reason=failure_reason):
                hint = preflight.runtime_repair_hint(
                    "verify-failed", failure_reason=failure_reason
                )
                self.assertIsNotNone(hint)
                assert hint is not None
                self.assertEqual(
                    f"runtime.verify-failed.{failure_reason}", hint.check_id
                )
                self.assertEqual("verify-failed", hint.category)

    def test_unknown_runtime_reason_returns_none(self) -> None:
        self.assertIsNone(preflight.runtime_repair_hint("nonexistent"))


class PreflightEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self._env_ctx = patched_env(
            XDG_CONFIG_HOME=str(self.xdg_config),
            XDG_STATE_HOME=str(self.xdg_state),
        )
        self._env_ctx.__enter__()
        self.plan_dir = self.tmp_path / "plan"
        _write_loop_ready_plan(self.plan_dir, ("pending",))

    def tearDown(self) -> None:
        self._env_ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _drop_executable(self, directory: Path, name: str) -> Path:
        target = directory / name
        target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        target.chmod(0o755)
        return target

    def _path_with(self, *names: str, base: Path | None = None) -> str:
        base = base or self.path_dir
        for name in names:
            self._drop_executable(base, name)
        return str(base)

    def test_env_repo_root_missing_emits_red(self) -> None:
        missing = self.tmp_path / "absent"
        report = preflight.run_environment_preflight(self.plan_dir, cwd=missing)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("does not exist", repo_findings[0].message)
        self.assertFalse(report.ok)

    def test_env_repo_root_not_directory_emits_red(self) -> None:
        file_cwd = self.tmp_path / "file.txt"
        file_cwd.write_text("nope", encoding="utf-8")
        report = preflight.run_environment_preflight(self.plan_dir, cwd=file_cwd)
        repo_findings = [f for f in report.findings if f.category == "env-repo-root"]
        self.assertEqual(1, len(repo_findings))
        self.assertEqual("red", repo_findings[0].severity)
        self.assertIn("not a directory", repo_findings[0].message)

    def test_env_git_outside_repo_emits_red(self) -> None:
        cwd = self.tmp_path / "non-git"
        cwd.mkdir()
        report = preflight.run_environment_preflight(self.plan_dir, cwd=cwd)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("red", git_findings[0].severity)
        self.assertIn("not inside a git repository", git_findings[0].message)

    def test_env_git_inside_repo_is_silent_without_commit_mode(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(self.plan_dir, cwd=repo_dir)
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_git_dirty_with_commit_mode_emits_yellow(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        (repo_dir / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="per-session"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0].severity)
        self.assertIn("dirty", git_findings[0].message)
        self.assertIn("per-session", git_findings[0].detail)

    def test_env_git_clean_with_commit_mode_is_silent(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        report = preflight.run_environment_preflight(
            self.plan_dir, cwd=repo_dir, commit_mode="end"
        )
        git_findings = [f for f in report.findings if f.category == "env-git"]
        self.assertEqual([], git_findings)

    def test_env_harness_unsupported_name_emits_red(self) -> None:
        report = preflight.run_environment_preflight(self.plan_dir, harness="bogus")
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("unsupported harness", harness_findings[0].message)

    def test_env_harness_missing_executable_emits_red(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, harness="codex"
            )
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual(1, len(harness_findings))
        self.assertEqual("red", harness_findings[0].severity)
        self.assertIn("not found on PATH", harness_findings[0].message)

    def test_env_harness_present_executable_is_silent(self) -> None:
        path_str = self._path_with("codex")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(
                self.plan_dir, harness="codex"
            )
        harness_findings = [f for f in report.findings if f.category == "env-harness"]
        self.assertEqual([], harness_findings)

    def test_env_spec_missing_emits_red(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, commit_mode="per-session"
            )
        spec_findings = [f for f in report.findings if f.category == "env-spec"]
        self.assertEqual(1, len(spec_findings))
        self.assertEqual("red", spec_findings[0].severity)
        self.assertIn("spec executable not found", spec_findings[0].message)

    def test_env_spec_present_is_silent(self) -> None:
        path_str = self._path_with("spec")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(
                self.plan_dir, commit_mode="end"
            )
        spec_findings = [f for f in report.findings if f.category == "env-spec"]
        self.assertEqual([], spec_findings)

    def test_env_lock_absent_emits_no_finding(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual([], lock_findings)

    def test_env_lock_stale_emits_yellow(self) -> None:
        plan_state = state.plan_state_dir(self.plan_dir, self.xdg_state)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.xdg_state)
        canonical_plan = plans.canonicalize_path(self.plan_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(canonical_plan),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0].severity)
        self.assertIn("stale lock", lock_findings[0].message)

    def test_env_lock_active_emits_red(self) -> None:
        lock_info = state.acquire_lock(self.plan_dir, self.xdg_state)
        try:
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        finally:
            state.release_lock(self.plan_dir, self.xdg_state)
        lock_findings = [f for f in report.findings if f.category == "env-lock"]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("red", lock_findings[0].severity)
        self.assertIn(str(lock_info.pid), lock_findings[0].message)

    def test_env_amx_present_emits_green(self) -> None:
        path_str = self._path_with("amx")
        with patched_env(PATH=path_str):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("available", amx_findings[0].message)

    def test_env_amx_absent_emits_green(self) -> None:
        empty_path = self.tmp_path / "empty-path"
        empty_path.mkdir()
        with patched_env(PATH=str(empty_path)):
            report = preflight.run_environment_preflight(
                self.plan_dir, state_dir=self.xdg_state
            )
        amx_findings = [f for f in report.findings if f.category == "env-amx"]
        self.assertEqual(1, len(amx_findings))
        self.assertEqual("green", amx_findings[0].severity)
        self.assertIn("not on PATH", amx_findings[0].message)

    def test_env_turn_budget_green_with_pending_count(self) -> None:
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("1 session(s) pending", budget_findings[0].message)

    def test_env_turn_budget_yellow_above_threshold(self) -> None:
        many_plan = self.tmp_path / "many-plan"
        _write_loop_ready_plan(
            many_plan,
            tuple(["pending"] * preflight.TURN_BUDGET_YELLOW_THRESHOLD),
        )
        report = preflight.run_environment_preflight(
            many_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("yellow", budget_findings[0].severity)
        self.assertIn("exceeds", budget_findings[0].message)

    def test_env_turn_budget_complete_plan_is_green(self) -> None:
        complete_plan = self.tmp_path / "complete-plan"
        _write_loop_ready_plan(complete_plan, ("complete", "complete"))
        report = preflight.run_environment_preflight(
            complete_plan, state_dir=self.xdg_state
        )
        budget_findings = [
            f for f in report.findings if f.category == "env-turn-budget"
        ]
        self.assertEqual(1, len(budget_findings))
        self.assertEqual("green", budget_findings[0].severity)
        self.assertIn("all sessions complete", budget_findings[0].message)

    def test_run_environment_preflight_emits_no_core_findings(self) -> None:
        # Break readiness so core would emit a red. Environment-only must skip it.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        report = preflight.run_environment_preflight(
            self.plan_dir, state_dir=self.xdg_state
        )
        categories = {f.category for f in report.findings}
        self.assertNotIn("manifest-readiness", categories)
        self.assertNotIn("plan-structure", categories)

    def test_run_preflight_composes_core_and_env(self) -> None:
        repo_dir = self.tmp_path / "repo"
        init_git_repo(repo_dir)
        # Prepend the controlled dir to system PATH so git remains discoverable
        # while the fake codex/spec/amx executables take precedence.
        for name in ("codex", "spec", "amx"):
            self._drop_executable(self.path_dir, name)
        path_str = os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])
        with patched_env(PATH=path_str):
            report = preflight.run_preflight(
                self.plan_dir,
                cwd=repo_dir,
                harness="codex",
                commit_mode="per-session",
                state_dir=self.xdg_state,
            )
        # Core fields surface.
        self.assertIsNotNone(report.snapshot)
        self.assertIsNotNone(report.readiness)
        self.assertEqual({"1"}, set(report.contracts_by_session))
        # Env findings are present (env-amx + env-turn-budget at minimum, both green).
        categories = {f.category for f in report.findings}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # No red/yellow → ok.
        self.assertTrue(
            report.ok,
            msg=[(f.category, f.severity, f.message) for f in report.findings],
        )


class PreflightCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.xdg_config = self.tmp_path / "xdg-config"
        self.xdg_state = self.tmp_path / "xdg-state"
        self.path_dir = self.tmp_path / "path"
        self.xdg_config.mkdir()
        self.xdg_state.mkdir()
        self.path_dir.mkdir()
        self.plan_dir = self.tmp_path / "plan"
        self.repo_root = self.tmp_path / "repo"
        init_git_repo(self.repo_root)
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme-1"}',
                    session="1",
                ),
            },
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _run_preflight(
        self,
        *,
        json_output: bool = True,
        cwd: Path | None = None,
        harness: str | None = None,
        commit_mode: str | None = None,
        mark_loop_ready: bool = False,
        path_value: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--state-dir",
            str(self.xdg_state),
        ]
        if cwd is not None:
            argv.extend(["--cwd", str(cwd)])
        if harness is not None:
            argv.extend(["--harness", harness])
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if json_output:
            argv.append("--json")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(self.xdg_config)
        env["XDG_STATE_HOME"] = str(self.xdg_state)
        if path_value is not None:
            env["PATH"] = path_value
        return subprocess.run(
            argv,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_preflight_json_ok_when_ready_plan(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("preflight", payload["command"])
        self.assertEqual("ok", payload["reason"])
        self.assertEqual(str(self.plan_dir), payload["plan_dir"])
        self.assertEqual(str(self.repo_root), payload["cwd"])
        self.assertEqual("codex", payload["harness"])
        self.assertEqual("per-session", payload["commit_mode"])
        self.assertFalse(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        # No red/yellow findings present.
        counts = payload["findings_by_severity"]
        self.assertEqual(0, counts["red"])
        self.assertEqual(0, counts["yellow"])
        # All green findings still surface (env-amx and env-turn-budget at minimum).
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("env-amx", categories)
        self.assertIn("env-turn-budget", categories)
        # Readiness and expected sha-256 surface as serializable values.
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["readiness"]["readiness_sha256"],
            payload["expected_readiness_sha256"],
        )

    def test_preflight_json_red_when_readiness_breaks(self) -> None:
        # Break readiness sha to produce a red manifest-readiness finding.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-red", payload["reason"])
        self.assertFalse(payload["paused"])
        categories = {finding["category"] for finding in payload["findings"]}
        self.assertIn("manifest-readiness", categories)
        # findings keep the dataclass shape exactly.
        sample = payload["findings"][0]
        for key in (
            "severity",
            "category",
            "message",
            "session_id",
            "path",
            "line",
            "detail",
        ):
            self.assertIn(key, sample)

    def test_preflight_json_yellow_when_ambiguity_only(self) -> None:
        # Inject a yellow ambiguity sentinel into session-1.md instructions.
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        text = text.replace("Instructions.", "Instructions. TODO: revisit later")
        session_path.write_text(text, encoding="utf-8")
        # Recompute readiness so manifest-readiness stays green.
        expected = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=expected,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        categories_by_severity = {
            (finding["severity"], finding["category"])
            for finding in payload["findings"]
        }
        self.assertIn(("yellow", "ambiguity-sentinel"), categories_by_severity)

    def test_preflight_mark_loop_ready_writes_manifest(self) -> None:
        # The fixture is already loop-ready; --mark-loop-ready refreshes the
        # checked_at timestamp once preflight succeeds.
        original_sha = plans.stable_readiness_sha256(self.plan_dir)
        original = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(original)
        self.assertEqual("loop-ready", original.state)
        self.assertEqual(original_sha, original.readiness_sha256)
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertTrue(payload["marked_loop_ready"])
        refreshed = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(refreshed)
        self.assertEqual("loop-ready", refreshed.state)
        self.assertEqual(original_sha, refreshed.readiness_sha256)
        self.assertNotEqual(original.checked_at, refreshed.checked_at)

    def test_preflight_mark_loop_ready_promotes_draft_or_reviewed(self) -> None:
        for readiness_state in ("draft", "reviewed"):
            with self.subTest(readiness_state=readiness_state):
                _write_loop_ready_plan(
                    self.plan_dir,
                    ("pending",),
                    contracts_by_id={
                        "1": _contract_block(
                            '{"type": "file_exists", "path": "README.md", '
                            '"name": "readme-1"}',
                            session="1",
                        ),
                    },
                    state=readiness_state,
                )

                completed = self._run_preflight(mark_loop_ready=True)

                self.assertEqual(0, completed.returncode, completed.stderr)
                payload = json.loads(completed.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual("loop-ready-marked", payload["reason"])
                self.assertTrue(payload["marked_loop_ready"])
                self.assertEqual("loop-ready", payload["readiness"]["state"])
                self.assertEqual(0, payload["findings_by_severity"]["red"])
                self.assertEqual(0, payload["findings_by_severity"]["yellow"])
                metadata = plans.read_readiness_metadata(self.plan_dir)
                self.assertIsNotNone(metadata)
                self.assertEqual("loop-ready", metadata.state)

    def test_preflight_mark_loop_ready_repairs_missing_readiness(self) -> None:
        write_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme-1"}',
                    session="1",
                )
            },
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(metadata)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_preflight_mark_loop_ready_repairs_stale_readiness_sha(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready-marked", payload["reason"])
        self.assertTrue(payload["marked_loop_ready"])
        self.assertEqual("loop-ready", payload["readiness"]["state"])
        self.assertEqual(
            payload["expected_readiness_sha256"],
            payload["readiness"]["readiness_sha256"],
        )

    def test_preflight_mark_loop_ready_refused_when_non_readiness_not_ok(
        self,
    ) -> None:
        session_path = self.plan_dir / "session-1.md"
        text = session_path.read_text(encoding="utf-8")
        session_path.write_text(
            text.replace("Instructions.", "Instructions. TODO: revisit later"),
            encoding="utf-8",
        )
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=plans.stable_readiness_sha256(self.plan_dir),
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        completed = self._run_preflight(mark_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual("preflight-yellow", payload["reason"])
        self.assertTrue(payload["mark_loop_ready_requested"])
        self.assertFalse(payload["marked_loop_ready"])
        self.assertIn("non-readiness", payload["mark_error"])
        readiness = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness)
        self.assertEqual("draft", readiness.state)

    def test_preflight_text_output_lists_findings(self) -> None:
        path_value = self._path_value("codex", "spec", "amx")
        completed = self._run_preflight(
            json_output=False,
            cwd=self.repo_root,
            harness="codex",
            commit_mode="per-session",
            path_value=path_value,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn(f"Plan: {self.plan_dir}", completed.stdout)
        self.assertIn("Sessions: 1", completed.stdout)
        self.assertIn("Harness: codex", completed.stdout)
        self.assertIn("Commit mode: per-session", completed.stdout)
        self.assertIn("Preflight: ok", completed.stdout)
        self.assertIn("[GREEN] env-amx", completed.stdout)
        self.assertIn("[GREEN] env-turn-budget", completed.stdout)

    def test_preflight_text_output_failure_routes_summary_to_stderr(self) -> None:
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight(json_output=False)
        self.assertEqual(1, completed.returncode)
        self.assertIn("[RED] manifest-readiness", completed.stdout)
        self.assertIn("Preflight: failed", completed.stderr)

    def test_preflight_json_includes_repair_hints_aligned_with_findings(
        self,
    ) -> None:
        # Break readiness to force a red manifest-readiness finding so we have
        # at least one finding to align a repair hint with.
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="00" * 32,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        completed = self._run_preflight()
        self.assertEqual(1, completed.returncode)
        payload = json.loads(completed.stdout)
        self.assertIn("repair_hints", payload)
        self.assertIsInstance(payload["repair_hints"], list)
        self.assertEqual(
            len(payload["findings"]), len(payload["repair_hints"])
        )
        # Each hint exposes the documented stable keys.
        for hint in payload["repair_hints"]:
            for key in (
                "category",
                "check_id",
                "severity_hint",
                "message",
                "question_template",
                "fix_category",
            ):
                self.assertIn(key, hint)
        # The manifest-readiness finding resolves to the sha-mismatch override.
        manifest_hints = [
            hint
            for hint, finding in zip(payload["repair_hints"], payload["findings"])
            if finding["category"] == "manifest-readiness"
        ]
        self.assertTrue(manifest_hints)
        self.assertEqual(
            "manifest-readiness.sha-mismatch", manifest_hints[0]["check_id"]
        )


class SkillInstructionTests(unittest.TestCase):
    def test_session_expand_auto_marks_loop_ready_before_session_run(self) -> None:
        text = (REPO_ROOT / "skills/session-expand/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "asl preflight <plan-dir> --cwd <codebase-root> --json --mark-loop-ready",
            text,
        )
        self.assertIn("marked_loop_ready", text)
        self.assertIn(
            "Only when final preflight succeeds with `ok: true` and readiness is `loop-ready`",
            text,
        )
        self.assertIn("<skill-prefix>plan-preflight-iterate", text)

    def test_session_split_names_current_contract_categories(self) -> None:
        text = (REPO_ROOT / "skills/session-split/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("missing-contract", text)
        self.assertNotIn("session-contract", text)


class RunReadinessGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...] = ("pending",),
        *,
        readiness: str | None = "loop-ready",
        sha_override: str | None = None,
    ) -> plans.ReadinessMetadata | None:
        contracts = {
            str(index): _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{index}"}}',
                session=str(index),
            )
            for index in range(1, len(statuses) + 1)
        }
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts)
        if readiness is None:
            return None
        sha = sha_override or plans.stable_readiness_sha256(self.plan_dir)
        metadata = plans.ReadinessMetadata(
            state=readiness,
            readiness_sha256=sha,
            checked_at="2026-06-19T00:00:00Z",
        )
        plans.write_readiness_metadata(self.plan_dir, metadata)
        return metadata

    def _run(
        self,
        *,
        mode: str = "success",
        max_sessions: int | None = None,
        allow_not_loop_ready: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_run_refuses_when_readiness_metadata_missing(self) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        self.assertIn("readiness metadata", payload["pause_message"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertIsNone(gate["readiness_state"])
        self.assertIsNone(payload["readiness_state_initial"])
        self.assertIsNone(payload["readiness_state_final"])
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_run_refuses_when_state_is_reviewed(self) -> None:
        self._write_plan(("pending",), readiness="reviewed")

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertEqual("reviewed", gate["readiness_state"])
        self.assertEqual("reviewed", payload["readiness_state_initial"])
        self.assertEqual(
            "reviewed", plans.read_readiness_metadata(self.plan_dir).state
        )

    def test_run_refuses_when_recorded_sha_does_not_match(self) -> None:
        self._write_plan(
            ("pending",),
            readiness="loop-ready",
            sha_override="0" * 64,
        )

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("not-loop-ready", payload["pause_reason"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertFalse(gate["preflight_ok"])
        self.assertEqual("0" * 64, gate["recorded_readiness_sha256"])
        self.assertNotEqual(
            gate["recorded_readiness_sha256"],
            gate["expected_readiness_sha256"],
        )
        manifest_readiness_findings = [
            finding
            for finding in gate["findings"]
            if finding["category"] == "manifest-readiness"
        ]
        self.assertTrue(manifest_readiness_findings)

    def test_allow_not_loop_ready_lets_run_proceed_and_reports_findings(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness=None)

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertTrue(payload["allow_not_loop_ready"])
        gate = payload["readiness_gate"]
        self.assertFalse(gate["ok"])
        self.assertIsNone(gate["readiness_state"])
        self.assertTrue(
            any(
                finding["category"] == "manifest-readiness"
                for finding in gate["findings"]
            )
        )
        # No readiness existed, so the lifecycle never engages.
        self.assertIsNone(payload["readiness_state_initial"])
        self.assertIsNone(payload["readiness_state_final"])
        self.assertIsNone(plans.read_readiness_metadata(self.plan_dir))

    def test_lifecycle_transitions_to_complete_when_all_sessions_finish(
        self,
    ) -> None:
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("complete", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_lifecycle_returns_to_loop_ready_when_max_sessions_leaves_pending(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")

        completed, payload = self._run(max_sessions=1)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            metadata.readiness_sha256,
        )

    def test_lifecycle_refreshes_running_sha_after_future_session_notes(
        self,
    ) -> None:
        self._write_plan(("pending", "pending"), readiness="loop-ready")
        original_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(original_metadata)

        completed, payload = self._run(
            mode="success-future-note",
            max_sessions=1,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertEqual("max-sessions", payload["reason"])
        self.assertEqual("loop-ready", payload["readiness_state_final"])
        self.assertIsNone(payload.get("readiness_state_refresh_error"))
        self.assertIsNotNone(payload.get("readiness_state_refreshed_at"))
        future_session = self.plan_dir / "session-2.md"
        self.assertIn(
            "Generated future-session note",
            future_session.read_text(encoding="utf-8"),
        )
        refreshed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("loop-ready", refreshed_metadata.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            refreshed_metadata.readiness_sha256,
        )
        self.assertNotEqual(
            original_metadata.readiness_sha256,
            refreshed_metadata.readiness_sha256,
        )

    def test_lifecycle_marks_reviewed_when_run_pauses_on_failure(self) -> None:
        self._write_plan(("pending",), readiness="loop-ready")

        completed, payload = self._run(mode="failure")

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("loop-ready", payload["readiness_state_initial"])
        self.assertEqual("reviewed", payload["readiness_state_final"])
        metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", metadata.state)


class RunVerificationEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _write_plan(
        self,
        statuses: tuple[str, ...],
        *,
        contracts_by_id: dict[str, str] | None,
        readiness: str | None = "loop-ready",
    ) -> None:
        write_plan(self.plan_dir, statuses, contracts_by_id=contracts_by_id)
        if readiness is None:
            return
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state=readiness,
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def _run(
        self,
        *,
        allow_not_loop_ready: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "FAKE_CODEX_MODE": "success",
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return completed, json.loads(completed.stdout)

    def test_verify_passing_contract_allows_session_to_complete(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                '"name": "readme"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        self.assertEqual("all-complete", payload["reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", text)
        turns = payload["turns"]
        self.assertEqual(1, len(turns))
        verify_payload = turns[0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verify_failing_contract_pauses_and_reverts_status(self) -> None:
        contracts_by_id = {
            "1": _contract_block(
                '{"type": "file_exists", "path": "MISSING.md", '
                '"name": "missing"}',
                session="1",
            )
        }
        self._write_plan(("pending",), contracts_by_id=contracts_by_id)

        completed, payload = self._run()

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("verify-failed", payload["pause_reason"])
        self.assertIn("verification failed", payload["pause_message"])
        pause_details = payload["pause_details"]
        self.assertFalse(pause_details["passed"])
        self.assertEqual(["missing"], pause_details["recipe"]["names"])
        self.assertEqual(1, len(pause_details["failures"]))
        failure = pause_details["failures"][0]
        self.assertEqual("missing", failure["name"])
        self.assertEqual("file_exists", failure["kind"])
        self.assertEqual("file-missing", failure["failure_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertNotIn("- **Status**: complete", text)
        self.assertIn("verification failed", text)
        self.assertIn("file-missing", text)
        final_snapshot = payload["final_snapshot"]
        self.assertEqual(1, final_snapshot["counts"]["pending"])
        self.assertEqual(0, final_snapshot["counts"]["complete"])

    def test_verify_missing_contract_pauses_when_gate_enforced(self) -> None:
        self._write_plan(("pending",), contracts_by_id=None)

        completed, payload = self._run(allow_not_loop_ready=False)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        # Preflight gate fails first because contracts are required for
        # loop-ready plans, so the runner pauses on `not-loop-ready` before
        # verification runs.
        self.assertEqual("not-loop-ready", payload["pause_reason"])

    def test_verify_missing_contract_is_skipped_when_gate_bypassed(self) -> None:
        self._write_plan(
            ("pending",), contracts_by_id=None, readiness=None
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["paused"])
        verify_payload = payload["turns"][0]["verification"]
        self.assertEqual("no-contract", verify_payload["reason"])
        self.assertFalse(verify_payload["enforced"])

    def test_verify_malformed_contract_pauses_with_malformed_contract(self) -> None:
        bad_contract = "\n".join(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "- recipe runs",
                "",
                "### Verify",
                "",
                "```json",
                '{"session": "1", "checks": [{"type": "command", '
                '"argv": ["echo"], "timeout_seconds": -1}]}',
                "```",
            ]
        )
        self._write_plan(
            ("pending",),
            contracts_by_id={"1": bad_contract},
            readiness=None,
        )

        completed, payload = self._run(allow_not_loop_ready=True)

        self.assertEqual(1, completed.returncode)
        self.assertTrue(payload["paused"])
        self.assertEqual("malformed-contract", payload["pause_reason"])
        session_file = self.plan_dir / "session-1.md"
        text = session_file.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", text)
        self.assertIn("verification skipped", text)

    def test_verify_failed_pause_is_listed_in_pause_reasons(self) -> None:
        self.assertIn("verify-failed", events.PAUSE_REASONS)
        self.assertIn("no-contract", events.PAUSE_REASONS)
        self.assertIn("malformed-contract", events.PAUSE_REASONS)


class EndToEndQATests(unittest.TestCase):
    """Synthetic end-to-end coverage that wires preflight + run + verify.

    Unit-level coverage already exercises every preflight finding, every
    runner pause reason, and every verify outcome individually. These tests
    intentionally chain those subsystems together through the CLI surface,
    because the loop-readiness contract introduced in this plan only holds
    when the pieces compose correctly. Each test is the integration analog
    of a smoke step from ``docs/agent-session-loop.md``.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.plan_dir = self.tmp_path / "plan"
        self.state_dir = self.tmp_path / "state"
        self.repo_root = self.tmp_path / "repo"
        self.config_home = self.tmp_path / "config"
        self.path_dir = self.tmp_path / "path"
        self.path_dir.mkdir()
        init_git_repo(self.repo_root)
        self.fake_codex = install_fake_codex(self.tmp_path)
        config_path = (
            self.config_home / config.APP_NAME / config.HARNESS_CONFIG_NAME
        )
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps({"codex": {"executable": str(self.fake_codex)}}),
            encoding="utf-8",
        )

    def _path_value(self, *names: str) -> str:
        for name in names:
            target = self.path_dir / name
            target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            target.chmod(0o755)
        return os.pathsep.join([str(self.path_dir), os.environ.get("PATH", "")])

    def _base_env(self, *, mode: str = "success") -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_dir),
                "FAKE_CODEX_MODE": mode,
                "FAKE_CODEX_SLEEP": "5",
                "PYTHONPATH": str(REPO_ROOT)
                + os.pathsep
                + env.get("PYTHONPATH", ""),
            }
        )
        return env

    def _run_preflight(
        self,
        *,
        mark_loop_ready: bool = False,
        commit_mode: str | None = None,
        path_value: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "preflight",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if commit_mode == "per-session":
            argv.append("--commit-per-session")
        elif commit_mode == "end":
            argv.append("--commit-at-end")
        if mark_loop_ready:
            argv.append("--mark-loop-ready")
        env = self._base_env()
        if path_value is not None:
            env["PATH"] = path_value
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout) if completed.stdout else {}
        return completed, payload

    def _run_run(
        self,
        *,
        allow_not_loop_ready: bool = False,
        max_sessions: int | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "run",
            str(self.plan_dir),
            "--cwd",
            str(self.repo_root),
            "--harness",
            "codex",
            "--state-dir",
            str(self.state_dir),
            "--json",
        ]
        if allow_not_loop_ready:
            argv.append("--allow-not-loop-ready")
        if max_sessions is not None:
            argv.extend(["--max-sessions", str(max_sessions)])
        completed = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            env=self._base_env(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        payload = json.loads(completed.stdout)
        return completed, payload

    def _write_legacy_plan(self) -> None:
        """A pre-loop-readiness plan: no manifest readiness, no contract."""
        write_plan(self.plan_dir, ("pending",))

    def _repair_legacy_plan_to_draft(self) -> None:
        """Repair legacy content while leaving readiness for preflight to mark."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="draft",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

    def test_full_lifecycle_repairs_legacy_plan_and_runs_to_completion(
        self,
    ) -> None:
        """Legacy plan → preflight red → repair → mark → run → verify pass."""
        self._write_legacy_plan()
        path_value = self._path_value("codex", "spec", "amx")

        # Step 1: preflight surfaces red findings on the legacy plan.
        before, before_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, before.returncode)
        self.assertFalse(before_payload["ok"])
        before_categories = {
            f["category"] for f in before_payload["findings"]
        }
        self.assertIn("manifest-readiness", before_categories)
        self.assertIn("missing-contract", before_categories)
        # Repair hints stay aligned with findings 1:1 for the guided skill.
        self.assertEqual(
            len(before_payload["findings"]),
            len(before_payload["repair_hints"]),
        )

        # Step 2: repair (add contract, write draft readiness).
        self._repair_legacy_plan_to_draft()
        readiness_before_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("draft", readiness_before_mark.state)

        # Step 3: --mark-loop-ready promotes a clean draft plan.
        marked, marked_payload = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(0, marked.returncode, marked.stderr)
        self.assertTrue(marked_payload["ok"])
        self.assertEqual("loop-ready-marked", marked_payload["reason"])
        self.assertTrue(marked_payload["marked_loop_ready"])
        readiness_after_mark = plans.read_readiness_metadata(self.plan_dir)
        self.assertIsNotNone(readiness_after_mark)
        self.assertEqual("loop-ready", readiness_after_mark.state)
        self.assertEqual(
            plans.stable_readiness_sha256(self.plan_dir),
            readiness_after_mark.readiness_sha256,
        )
        self.assertNotEqual(readiness_before_mark.state, readiness_after_mark.state)

        # Step 4: asl run completes the plan with verification passing.
        run_completed, run_payload = self._run_run()
        self.assertEqual(0, run_completed.returncode, run_completed.stderr)
        self.assertTrue(run_payload["ok"])
        self.assertFalse(run_payload["paused"])
        self.assertEqual("all-complete", run_payload["reason"])
        self.assertEqual("complete", run_payload["readiness_state_final"])
        session_text = (self.plan_dir / "session-1.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- **Status**: complete", session_text)
        verify_payload = run_payload["turns"][0]["verification"]
        self.assertEqual("ok", verify_payload["reason"])
        self.assertTrue(verify_payload["passed"])

    def test_verification_failure_pauses_then_resumes_after_repair(self) -> None:
        """`verify-failed` reverts status; re-running after repair completes."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "later.txt", "name": "later"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )

        # First run: verify fails, status reverts to pending.
        first_completed, first_payload = self._run_run()
        self.assertEqual(1, first_completed.returncode)
        self.assertTrue(first_payload["paused"])
        self.assertEqual("verify-failed", first_payload["pause_reason"])
        pause_details = first_payload["pause_details"]
        self.assertEqual(["later"], pause_details["recipe"]["names"])
        self.assertEqual("file-missing", pause_details["failures"][0][
            "failure_reason"
        ])
        session_path = self.plan_dir / "session-1.md"
        session_text = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: pending", session_text)
        self.assertIn("verification failed", session_text)

        # A verify-failed pause flips readiness to `reviewed`; after the
        # underlying issue is repaired, preflight can re-mark the plan.
        reviewed_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("reviewed", reviewed_metadata.state)

        # Repair: create the missing file so the contract will now pass.
        (self.repo_root / "later.txt").write_text("done\n", encoding="utf-8")

        remark_completed, remark_payload = self._run_preflight(mark_loop_ready=True)
        self.assertEqual(0, remark_completed.returncode, remark_completed.stderr)
        self.assertTrue(remark_payload["marked_loop_ready"])
        self.assertEqual("loop-ready", remark_payload["readiness"]["state"])

        # Second run: the still-pending session is picked up and completes.
        second_completed, second_payload = self._run_run()
        self.assertEqual(0, second_completed.returncode, second_completed.stderr)
        self.assertTrue(second_payload["ok"])
        self.assertFalse(second_payload["paused"])
        self.assertEqual("all-complete", second_payload["reason"])
        final_session = session_path.read_text(encoding="utf-8")
        self.assertIn("- **Status**: complete", final_session)
        # Readiness lifecycle resolves to complete after the resumed run.
        final_metadata = plans.read_readiness_metadata(self.plan_dir)
        self.assertEqual("complete", final_metadata.state)

    def test_stale_lock_surfaces_in_preflight_cli_with_repair_hint(
        self,
    ) -> None:
        """A stale lock JSON-fingerprints to env-lock.stale via the CLI."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        plan_state = state.plan_state_dir(self.plan_dir, self.state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)
        lock_path = state.lock_path_for_plan(self.plan_dir, self.state_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "pid": 0,
                    "plan_dir": str(plans.canonicalize_path(self.plan_dir)),
                    "created_at": "2026-06-19T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        lock_findings = [
            f for f in payload["findings"] if f["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_findings))
        self.assertEqual("yellow", lock_findings[0]["severity"])
        self.assertIn("stale lock", lock_findings[0]["message"])
        # Each finding pairs with a stable repair hint check id.
        lock_hints = [
            hint
            for hint, finding in zip(
                payload["repair_hints"], payload["findings"]
            )
            if finding["category"] == "env-lock"
        ]
        self.assertEqual(1, len(lock_hints))
        self.assertEqual("env-lock.stale", lock_hints[0]["check_id"])

    def test_commit_mode_dirty_worktree_emits_yellow_via_preflight_cli(
        self,
    ) -> None:
        """`--commit-per-session` plus a dirty worktree surfaces env-git yellow."""
        _write_loop_ready_plan(
            self.plan_dir,
            ("pending",),
            contracts_by_id={
                "1": _contract_block(
                    '{"type": "file_exists", "path": "README.md", '
                    '"name": "readme"}',
                    session="1",
                )
            },
        )
        (self.repo_root / "dirty.txt").write_text(
            "uncommitted\n", encoding="utf-8"
        )
        path_value = self._path_value("codex", "spec", "amx")
        completed, payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(1, completed.returncode)
        self.assertFalse(payload["ok"])
        git_findings = [
            f for f in payload["findings"] if f["category"] == "env-git"
        ]
        self.assertEqual(1, len(git_findings))
        self.assertEqual("yellow", git_findings[0]["severity"])
        self.assertIn("dirty", git_findings[0]["message"])
        # Marking loop-ready must be refused while yellow findings remain.
        completed_mark, payload_mark = self._run_preflight(
            commit_mode="per-session",
            mark_loop_ready=True,
            path_value=path_value,
        )
        self.assertEqual(1, completed_mark.returncode)
        self.assertFalse(payload_mark["marked_loop_ready"])

    def test_json_contracts_share_stable_keys_across_preflight_and_verify(
        self,
    ) -> None:
        """The JSON shape published in the docs must hold at the CLI surface."""
        contract = _contract_block(
            '{"type": "file_exists", "path": "README.md", "name": "readme"}',
            session="1",
        )
        write_plan(
            self.plan_dir, ("pending",), contracts_by_id={"1": contract}
        )
        sha = plans.stable_readiness_sha256(self.plan_dir)
        plans.write_readiness_metadata(
            self.plan_dir,
            plans.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256=sha,
                checked_at="2026-06-19T00:00:00Z",
            ),
        )
        path_value = self._path_value("codex", "spec", "amx")

        # Preflight JSON: documented top-level keys.
        preflight_completed, preflight_payload = self._run_preflight(
            commit_mode="per-session", path_value=path_value
        )
        self.assertEqual(0, preflight_completed.returncode)
        for key in (
            "ok",
            "command",
            "reason",
            "findings",
            "findings_by_severity",
            "repair_hints",
            "readiness",
            "expected_readiness_sha256",
            "mark_loop_ready_requested",
            "marked_loop_ready",
        ):
            self.assertIn(key, preflight_payload)
        self.assertEqual("preflight", preflight_payload["command"])

        # Verify JSON: documented top-level keys including stable reason enum.
        verify_argv = [
            sys.executable,
            "-m",
            "agent_session_loop",
            "verify",
            str(self.plan_dir),
            "--session",
            "1",
            "--cwd",
            str(self.repo_root),
            "--json",
        ]
        verify_completed = subprocess.run(
            verify_argv,
            cwd=REPO_ROOT,
            env=self._base_env(),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, verify_completed.returncode, verify_completed.stderr)
        verify_payload = json.loads(verify_completed.stdout)
        for key in (
            "ok",
            "command",
            "reason",
            "passed",
            "recipe",
            "checks",
            "session",
            "cwd",
            "plan_dir",
        ):
            self.assertIn(key, verify_payload)
        self.assertEqual("verify", verify_payload["command"])
        self.assertEqual("ok", verify_payload["reason"])


if __name__ == "__main__":
    unittest.main()
