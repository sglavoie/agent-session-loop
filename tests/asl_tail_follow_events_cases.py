from unittest import mock

from .asl_tail_support import *


class FollowEventsTests(unittest.TestCase):
    """The reusable `follow_events` JSONL follower feeds the ASL events tailer."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="asl-follow-events-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "events.jsonl"

    def _append(self, *lines: str) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def _record(self, name: str, **payload) -> str:
        return json.dumps({"name": name, "payload": payload})

    def test_missing_file_yields_empty_batches_until_it_appears(self) -> None:
        follower = tail.follow_events(self.path)
        self.assertEqual(next(follower), [])
        self._append(self._record("turn_heartbeat", session_id="1"))
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["name"], "turn_heartbeat")

    def test_incremental_append_only_yields_new_records(self) -> None:
        self._append(self._record("turn_started", session_id="1"))
        follower = tail.follow_events(self.path)
        first = next(follower)
        self.assertEqual([r["name"] for r in first], ["turn_started"])
        # Nothing new appended -> empty batch, no re-delivery of prior records.
        self.assertEqual(next(follower), [])
        self._append(self._record("turn_heartbeat", session_id="1", idle_seconds=5))
        second = next(follower)
        self.assertEqual([r["name"] for r in second], ["turn_heartbeat"])

    def test_garbage_and_non_object_lines_are_skipped(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        self._append(
            "",
            "not json at all",
            "[1, 2, 3]",
            "  ",
            self._record("turn_heartbeat", session_id="7"),
        )
        batch = next(follower)
        self.assertEqual([r["name"] for r in batch], ["turn_heartbeat"])
        self.assertEqual(batch[0]["payload"]["session_id"], "7")

    def test_versionless_v1_records_are_surfaced_verbatim(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        # A v1 record carries no `version` key; the follower surfaces it as-is.
        self._append(
            json.dumps({"name": "turn_heartbeat", "payload": {"idle_seconds": 3}})
        )
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertNotIn("version", batch[0])
        self.assertEqual(batch[0]["payload"]["idle_seconds"], 3)

    def test_partial_trailing_line_is_buffered_until_newline(self) -> None:
        follower = tail.follow_events(self.path)
        next(follower)
        # Write a record with no trailing newline yet (mid-append).
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "turn_heartbeat", "payload": {"idle_seconds"')
        self.assertEqual(next(follower), [])
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(": 9}}\n")
        batch = next(follower)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["payload"]["idle_seconds"], 9)

    def test_truncation_reopens_from_start(self) -> None:
        # Several records first, so the read offset is well past the length of
        # the short line the file is later truncated to.
        self._append(
            *(
                self._record("turn_heartbeat", session_id="1", idle_seconds=n)
                for n in range(5)
            )
        )
        follower = tail.follow_events(self.path)
        next(follower)
        # Rewrite the file smaller than the prior read offset (truncation).
        self.path.write_text(
            self._record("run_paused", reason="x") + "\n",
            encoding="utf-8",
        )
        batch = next(follower)
        self.assertEqual([r["name"] for r in batch], ["run_paused"])

    def test_rotation_to_a_new_inode_reopens_from_start(self) -> None:
        self._append(self._record("turn_heartbeat", session_id="1"))
        follower = tail.follow_events(self.path)
        next(follower)
        # Replace the file with a brand-new inode (rotation).
        rotated = self.tmp / "events.next"
        rotated.write_text(
            self._record("turn_heartbeat", session_id="2", idle_seconds=1)
            + "\n"
            + self._record("turn_heartbeat", session_id="2", idle_seconds=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(rotated, self.path)
        batch = next(follower)
        self.assertEqual([r["payload"]["session_id"] for r in batch], ["2", "2"])

    def test_rotation_between_stat_and_open_reads_new_file_from_start(self) -> None:
        self._append(self._record("turn_heartbeat", session_id="1"))
        follower = tail.follow_events(self.path)
        next(follower)
        self._append(self._record("turn_heartbeat", session_id="old"))

        replacement = self.tmp / "events.next"
        replacement.write_text(
            self._record("turn_heartbeat", session_id="2")
            + "\n"
            + self._record("run_paused", reason="new")
            + "\n",
            encoding="utf-8",
        )
        original_open = Path.open
        replaced = False

        def rotate_on_open(path: Path, *args, **kwargs):
            nonlocal replaced
            if path == self.path and args and args[0] == "rb" and not replaced:
                os.replace(replacement, self.path)
                replaced = True
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", rotate_on_open):
            batch = next(follower)

        self.assertTrue(replaced)
        self.assertEqual([r["name"] for r in batch], ["turn_heartbeat", "run_paused"])
        self.assertEqual(batch[0]["payload"]["session_id"], "2")
        self.assertEqual(next(follower), [])


if __name__ == "__main__":
    unittest.main()
