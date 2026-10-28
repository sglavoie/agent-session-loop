"""PlanMetadataTests and EventPersistenceTests.

Split from tests/test_asl_core.py."""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    artifacts,
    authoring_transaction,
    durability,
    events,
    handoff_guard_sessions,
    plans_content_hash,
    plans_errors,
    plans_files,
    plans_metadata,
    plans_models,
    plans_readiness,
    plans_status_tables,
    review_delta,
    review_session_body,
    tail_logs,
    turn_preparation_review,
    workspace_evidence_scope_repair,
)
from tests.support import (
    write_plan,
)


class PlanMetadataTests(unittest.TestCase):
    def test_round_trip_uses_canonical_absolute_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            plan_dir.mkdir()
            cwd.mkdir()

            path = plans_metadata.save_plan_metadata(plan_dir, cwd / ".." / "repo")
            metadata = plans_metadata.load_plan_metadata(plan_dir)

            self.assertEqual(
                plans_files.canonicalize_path(plan_dir)
                / plans_metadata.PLAN_METADATA_FILE_NAME,
                path,
            )
            self.assertEqual(
                plans_metadata.PlanMetadata(cwd=str(cwd.resolve())), metadata
            )
            self.assertEqual(
                {"cwd": str(cwd.resolve()), "version": 1},
                json.loads(path.read_text(encoding="utf-8")),
            )

    def test_missing_returns_none_and_malformed_metadata_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp)
            self.assertIsNone(plans_metadata.load_plan_metadata(plan_dir))
            path = plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME
            path.write_text('{"version": 1, "cwd": "relative"}\n', encoding="utf-8")
            with self.assertRaisesRegex(plans_errors.PlanError, "canonical absolute"):
                plans_metadata.load_plan_metadata(plan_dir)

    def test_metadata_does_not_change_plan_or_readiness_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_dir = root / "plan"
            cwd = root / "repo"
            write_plan(plan_dir, ("pending",))
            cwd.mkdir()
            content_before = plans_content_hash.plan_content_sha256(plan_dir)
            readiness_before = plans_content_hash.stable_readiness_sha256(plan_dir)

            plans_metadata.save_plan_metadata(plan_dir, cwd)

            self.assertEqual(
                content_before, plans_content_hash.plan_content_sha256(plan_dir)
            )
            self.assertEqual(
                readiness_before, plans_content_hash.stable_readiness_sha256(plan_dir)
            )


class EventPersistenceTests(unittest.TestCase):
    def test_durable_atomic_write_fullsyncs_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            with (
                mock.patch.object(artifacts.durability, "fullsync") as fullsync,
            ):
                artifacts.atomic_write_bytes(target, b"{}\n", durable=True)

            self.assertEqual(b"{}\n", target.read_bytes())
            self.assertEqual(2, fullsync.call_count)

    def test_replace_bytes_stages_per_thread_and_cleans_up_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            staging = durability.staging_path(target)
            self.assertIn(str(os.getpid()), staging.name)
            self.assertIn(str(threading.get_ident()), staging.name)
            target.write_bytes(b"old")
            with (
                mock.patch.object(durability.os, "replace", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                durability.replace_bytes(target, b"new")
            self.assertEqual(b"old", target.read_bytes())
            self.assertEqual(["state.json"], [p.name for p in Path(tmp).iterdir()])

    def test_write_text_is_an_atomic_replace(self) -> None:
        # `last-message.txt` is read for a pause signal, so a reader must see
        # the previous message or the whole new one, never a truncated file.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "turn" / "last-message.txt"
            durability.write_text(target, "old")
            inode = target.stat().st_ino
            with (
                mock.patch.object(durability.os, "replace", side_effect=OSError),
                self.assertRaises(OSError),
            ):
                durability.write_text(target, "new")
            self.assertEqual("old", target.read_text(encoding="utf-8"))
            self.assertEqual(
                ["last-message.txt"], [p.name for p in target.parent.iterdir()]
            )
            durability.write_text(target, "new")
            self.assertEqual("new", target.read_text(encoding="utf-8"))
            self.assertNotEqual(inode, target.stat().st_ino)

    def test_a_log_replaced_whole_is_followed_from_where_it_was(self) -> None:
        # The turn's final stdout write replaces the streamed log with a new
        # inode. The follower reopens by path and seeks its offset, so it
        # prints only what the replacement added rather than the whole log.
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "stdout.log"
            durability.append_text(log, "one\ntwo\n")
            target = {
                "path": log,
                "stream": "stdout",
                "session": "1",
                "turn_index": 1,
            }
            offsets: dict[str, int] = {}
            self.assertEqual(2, len(tail_logs._read_new_log_text(target, offsets)))
            durability.write_text(log, "one\ntwo\nasl: harness terminated\n")
            lines = tail_logs._read_new_log_text(target, offsets)
            self.assertEqual(1, len(lines))
            self.assertTrue(lines[0].endswith("asl: harness terminated"))

    def test_a_replaced_log_with_a_changed_prefix_replays_new_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "stdout.log"
            durability.append_text(log, "old first\nold second\n")
            target = {
                "path": log,
                "stream": "stdout",
                "session": "1",
                "turn_index": 1,
            }
            offsets: dict[str, int] = {}
            self.assertEqual(2, len(tail_logs._read_new_log_text(target, offsets)))

            durability.write_text(log, "new first\nnew second\nnew third\n")
            self.assertEqual(
                [
                    "stdout session=1 turn=1 | new first",
                    "stdout session=1 turn=1 | new second",
                    "stdout session=1 turn=1 | new third",
                ],
                tail_logs._read_new_log_text(target, offsets),
            )

    def test_a_log_line_is_followed_whole_and_a_truncated_log_from_the_start(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "stdout.log"
            log.write_bytes(b"")
            target = {"path": log, "stream": "stdout", "session": "1", "turn_index": 1}
            offsets: dict[str, int] = {}
            # One line written in two appends, the second split inside "é".
            with log.open("ab") as handle:
                handle.write(b"caf\xc3")
            self.assertEqual([], tail_logs._read_new_log_text(target, offsets))
            with log.open("ab") as handle:
                handle.write(b"\xa9 done\nnext")
            self.assertEqual(
                ["stdout session=1 turn=1 | café done"],
                tail_logs._read_new_log_text(target, offsets),
            )
            # A truncated log is read again from its start.
            log.write_bytes(b"fresh\n")
            self.assertEqual(
                ["stdout session=1 turn=1 | fresh"],
                tail_logs._read_new_log_text(target, offsets),
            )
            # The final read flushes an unterminated last line.
            with log.open("ab") as handle:
                handle.write(b"tail end")
            self.assertEqual([], tail_logs._read_new_log_text(target, offsets))
            self.assertEqual(
                ["stdout session=1 turn=1 | tail end"],
                tail_logs._read_new_log_text(target, offsets, final=True),
            )

    def test_plan_markdown_writers_replace_rather_than_rewrite_in_place(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_dir = Path(tmp) / "plan"
            write_plan(plan_dir, ("pending",))
            manifest = plan_dir / "manifest.md"
            before = manifest.read_bytes()
            metadata = plans_models.ReadinessMetadata(
                state="loop-ready",
                readiness_sha256="deadbeef",
                checked_at="2026-06-19T00:00:00Z",
            )
            with (
                mock.patch.object(durability.os, "replace", side_effect=OSError),
                self.assertRaises(plans_errors.PlanError),
            ):
                plans_readiness.write_readiness_metadata(plan_dir, metadata)
            self.assertEqual(before, manifest.read_bytes())
            self.assertEqual(
                [], [p.name for p in plan_dir.iterdir() if p.name.endswith(".tmp")]
            )

    def test_no_plan_or_session_writer_rewrites_a_file_in_place(self) -> None:
        # A plain `Path.write_text` truncates before it writes, so a crash or a
        # concurrent reader (`find_pending_generated_review_session`, the
        # readiness hash) can see a partial plan file.
        for module in (
            authoring_transaction,
            handoff_guard_sessions,
            plans_files,
            plans_readiness,
            plans_status_tables,
            review_delta,
            review_session_body,
            turn_preparation_review,
            workspace_evidence_scope_repair,
        ):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__ or "").read_text(encoding="utf-8")
                self.assertNotIn(".write_text(", source)
                self.assertNotIn(".write_bytes(", source)

    def test_staging_path_fits_name_max_for_a_maximal_name(self) -> None:
        target = Path("/nonexistent") / ("a" * 255)
        staging = durability.staging_path(target)
        self.assertLessEqual(len(os.fsencode(staging.name)), 255)
        self.assertNotEqual(staging, durability.staging_path(target.with_name("b")))

    def test_replace_bytes_applies_exact_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shim"
            durability.replace_text(target, "#!/bin/sh\n", permissions=0o755)
            self.assertEqual(0o755, target.stat().st_mode & 0o777)

    def test_replace_bytes_restricts_reused_staging_file_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "private.json"
            staging = durability.staging_path(target)
            staging.write_bytes(b"stale")
            staging.chmod(0o644)
            real_fchmod = os.fchmod

            def check_before_write(fd: int, permissions: int) -> None:
                self.assertEqual(b"stale", staging.read_bytes())
                real_fchmod(fd, permissions)

            with mock.patch.object(
                durability.os, "fchmod", side_effect=check_before_write
            ) as fchmod:
                durability.replace_bytes(target, b"private", permissions=0o600)

            fchmod.assert_called_once()
            self.assertEqual(b"private", target.read_bytes())
            self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_replace_bytes_rejects_staging_links_without_touching_their_target(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"
            victim = Path(tmp) / "other.json"
            staging = durability.staging_path(target)
            target.write_bytes(b"old state")
            victim.write_bytes(b"unrelated")

            for kind in ("symlink", "hardlink"):
                with self.subTest(kind=kind):
                    if kind == "symlink":
                        staging.symlink_to(victim)
                    else:
                        os.link(victim, staging)
                    with self.assertRaises(OSError):
                        durability.replace_bytes(target, b"new state")
                    self.assertEqual(b"old state", target.read_bytes())
                    self.assertEqual(b"unrelated", victim.read_bytes())
                    self.assertFalse(staging.exists())

    def test_fullsync_uses_device_barrier_on_darwin(self) -> None:
        with (
            mock.patch.object(durability.sys, "platform", "darwin"),
            mock.patch.object(durability.fcntl, "F_FULLFSYNC", 123, create=True),
            mock.patch.object(durability.fcntl, "fcntl") as full_fsync,
            mock.patch.object(durability.os, "fsync") as fsync,
        ):
            durability.fullsync(17)

        full_fsync.assert_called_once_with(17, 123)
        fsync.assert_not_called()

    def test_fullsync_falls_back_for_unsupported_darwin_filesystem(self) -> None:
        def unsupported(_fd: int, _command: int) -> None:
            raise OSError(errno.ENOTSUP, "Operation not supported")

        original_fsync = os.fsync
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            artifact_path = Path(tmp) / "artifact.json"
            with (
                mock.patch.object(durability.sys, "platform", "darwin"),
                mock.patch.object(durability.fcntl, "F_FULLFSYNC", 123, create=True),
                mock.patch.object(durability.fcntl, "fcntl", side_effect=unsupported),
                mock.patch.object(
                    durability.os, "fsync", wraps=original_fsync
                ) as fsync,
            ):
                events.append_event(event_path, events.Event(name="probe", payload={}))
                artifacts.atomic_write_bytes(artifact_path, b"{}\n", durable=True)

            self.assertTrue(event_path.read_text(encoding="utf-8").strip())
            self.assertEqual(b"{}\n", artifact_path.read_bytes())
            self.assertEqual(3, fsync.call_count)

    def test_append_event_starts_a_new_line_after_a_torn_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            events.append_event(event_path, events.Event(name="first", payload={}))
            with event_path.open("ab") as handle:
                handle.write(b'{"created_at": "2026-10-25T00:00:00+00:00", "na')

            events.append_event(event_path, events.Event(name="after", payload={}))

            lines = event_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(3, len(lines))
            self.assertEqual("first", json.loads(lines[0])["name"])
            self.assertEqual("after", json.loads(lines[2])["name"])

    def test_append_event_writes_complete_json_lines_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event_path = Path(tmp) / events.EVENTS_FILE_NAME
            # A worker that outlives the test body would keep calling
            # append_event -- and therefore fullsync -- while the next two tests
            # patch it and assert exact call counts. The stop flag is what makes
            # the hang guard below able to actually reclaim the threads.
            stop = threading.Event()

            def append_many(worker: int) -> None:
                for index in range(50):
                    if stop.is_set():
                        return
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
            try:
                # Darwin uses one device-level F_FULLFSYNC per record, and all
                # 400 barriers serialize behind one lock. This is a hang guard,
                # not a latency budget: the full suite runs this shard alongside
                # a hundred others, so budget generously.
                deadline = time.monotonic() + 180.0
                for thread in threads:
                    thread.join(timeout=max(0.0, deadline - time.monotonic()))
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=30)
            self.assertEqual([], [t.name for t in threads if t.is_alive()])

            lines = event_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(400, len(lines))
            records = [json.loads(line) for line in lines]
            observed = {
                (record["payload"]["worker"], record["payload"]["index"])
                for record in records
            }
            expected = {(worker, index) for worker in range(8) for index in range(50)}
            self.assertEqual(expected, observed)
