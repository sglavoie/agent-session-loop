from __future__ import annotations

# ruff: noqa: F403, F405
from .workbench_history_support import *


class IncrementalReadTests(HistoryTestCase):
    def test_a_committed_pass_does_not_re_read_what_it_consumed(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        first = self.run_import()
        self.assertEqual(len(first.entries), 1)
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(second.entries, ())
        self.assertTrue(second.complete)

    def test_an_idle_corpus_yields_an_empty_complete_pass(self) -> None:
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.sources, 0)
        self.assertTrue(result.complete)

    def test_a_crash_before_the_commit_re_delivers_rather_than_loses(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        first = self.run_import()
        # No commit: the upload is presumed to have failed.
        second = self.run_import()
        self.assertEqual(
            [entry.to_payload() for entry in first.entries],
            [entry.to_payload() for entry in second.entries],
        )
        self.assertEqual(first.entries[0].entry_id, second.entries[0].entry_id)

    def test_a_partial_final_line_is_never_consumed(self) -> None:
        path = self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20)],
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_finished("run-2", at=30))[:40])
        result = self.run_import()
        self.assertEqual([entry.run_id for entry in result.entries], ["run-1"])
        self.assertEqual(result.malformed, 0)
        # The half-written bytes are deferred, so the pass says it is partial.
        self.assertGreater(result.deferred_bytes, 0)
        self.assertFalse(result.complete)

    def test_a_bounded_pass_leaves_the_rest_for_the_next_one(self) -> None:
        self.write_events(
            [_started("run-1", at=10), _finished("run-1", at=20)], name="plan-a"
        )
        self.write_events(
            [
                _started("run-2", at=11, plan_dir=_OTHER_PLAN),
                _finished("run-2", at=21),
            ],
            name="plan-b",
        )
        first = self.run_import(max_pass_bytes=1)
        self.assertEqual(len(first.entries), 1)
        self.assertGreater(first.deferred_bytes, 0)
        self.assertFalse(first.complete)
        workbench_history.commit(self.store, first)
        second = self.run_import()
        self.assertEqual(len(second.entries), 1)
        self.assertTrue(second.complete)

    def test_a_malformed_line_is_counted_and_stepped_over(self) -> None:
        path = self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"name": "run_finished", "payload": broken}\n')
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.malformed, 1)
        self.assertFalse(result.complete)

    def test_a_non_finite_number_is_refused_rather_than_imported(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f'{{"created_at": "{_at(20)}", "name": "run_finished", '
                '"version": 2, "payload": {"run_id": "run-1", "ok": true, '
                '"paused": false, "reason": "all-complete", '
                '"duration_seconds": NaN}}\n'
            )
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.malformed, 1)

    def test_an_unsupported_event_version_is_skipped_not_guessed(self) -> None:
        record = _finished("run-1", at=20)
        record["version"] = 99
        self.write_events([_started("run-1", at=10), record])
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.unsupported, 1)
        self.assertFalse(result.complete)

    def test_a_version_1_record_is_read_and_marked_as_such(self) -> None:
        start = _started("run-1", at=10)
        del start["version"]
        end = _finished("run-1", at=20)
        del end["version"]
        self.write_events([start, end])
        entry = self.run_import().entries[0]
        self.assertEqual(entry.event_schema_version, 1)

    def test_a_line_past_the_record_ceiling_is_never_parsed(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    _finished("run-1", at=20, transcript="x" * history.MAX_RECORD_BYTES)
                )
                + "\n"
            )
        result = self.run_import()
        self.assertEqual(result.entries, ())
        self.assertEqual(result.oversized, 1)
        self.assertFalse(result.complete)

    def test_a_record_longer_than_a_pass_window_does_not_stall_the_reader(self) -> None:
        path = self.write_events([_started("run-1", at=10)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"name": "run_started", "filler": "y" * 8192}) + "\n"
            )
            handle.write(json.dumps(_finished("run-1", at=20)) + "\n")
        first = self.run_import(max_read_bytes=512, max_pass_bytes=1024 * 1024)
        workbench_history.commit(self.store, first)
        found = list(first.entries)
        for _ in range(8):
            if found:
                break
            passed = self.run_import(max_read_bytes=512, max_pass_bytes=1024 * 1024)
            workbench_history.commit(self.store, passed)
            found = list(passed.entries)
        self.assertEqual([entry.run_id for entry in found], ["run-1"])


# -- File identity ----------------------------------------------------------


class FileIdentityTests(HistoryTestCase):
    def _consume_everything(self) -> history.ImportResult:
        result = self.run_import()
        workbench_history.commit(self.store, result)
        return result

    def test_an_unchanged_file_is_not_restarted(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        second = self.run_import()
        self.assertEqual(second.restarted_sources, 0)

    def test_a_replaced_file_is_read_from_the_start(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        path = self.source_dir() / history.EVENTS_FILE_NAME
        replacement = path.with_name("events.new")
        replacement.write_text(
            json.dumps(_started("run-2", at=30))
            + "\n"
            + json.dumps(_finished("run-2", at=40))
            + "\n",
            encoding="utf-8",
        )
        os.replace(replacement, path)
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-2"])

    def test_a_truncated_file_is_read_from_the_start(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20),
                _started("run-2", at=30),
                _finished("run-2", at=40),
            ]
        )
        self._consume_everything()
        self.write_events([_started("run-3", at=50), _finished("run-3", at=60)])
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-3"])

    def test_a_file_rewritten_in_place_at_the_same_length_is_detected(self) -> None:
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self._consume_everything()
        path = self.source_dir() / history.EVENTS_FILE_NAME
        original = path.stat().st_size
        rewritten = json.dumps(_started("run-9", at=10)) + "\n"
        rewritten += json.dumps(_finished("run-9", at=20)) + "\n"
        padding = original - len(rewritten.encode("utf-8"))
        if padding > 0:
            rewritten += " " * (padding - 1) + "\n"
        with path.open("r+b") as handle:
            handle.write(rewritten.encode("utf-8"))
            handle.truncate(max(original, len(rewritten.encode("utf-8"))))
        result = self.run_import()
        self.assertEqual(result.restarted_sources, 1)
        self.assertEqual([entry.run_id for entry in result.entries], ["run-9"])

    def test_an_unreadable_source_makes_the_pass_incomplete(self) -> None:
        path = self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o600)
        result = self.run_import()
        self.assertEqual(result.unreadable_sources, 1)
        self.assertFalse(result.complete)
        self.assertIn(
            "1 ASL plan(s) could not be read during this import", result.warnings
        )


# -- Duplicates -------------------------------------------------------------


class DuplicateTests(HistoryTestCase):
    def test_the_same_ending_appended_twice_becomes_one_entry(self) -> None:
        self.write_events(
            [
                _started("run-1", at=10),
                _finished("run-1", at=20),
                _finished("run-1", at=21),
            ]
        )
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.duplicates, 1)

    def test_a_restart_after_replacement_does_not_re_deliver(self) -> None:
        records = [_started("run-1", at=10), _finished("run-1", at=20)]
        self.write_events(records)
        first = self.run_import()
        workbench_history.commit(self.store, first)
        path = self.source_dir() / history.EVENTS_FILE_NAME
        replacement = path.with_name("events.new")
        replacement.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        os.replace(replacement, path)
        second = self.run_import()
        self.assertEqual(second.restarted_sources, 1)
        self.assertEqual(second.entries, ())
        self.assertEqual(second.duplicates, 1)

    def test_one_run_seen_through_two_state_directories_is_one_entry(self) -> None:
        records = [_started("run-1", at=10), _finished("run-1", at=20)]
        self.write_events(records, name="plan-a")
        self.write_events(records, name="plan-b")
        result = self.run_import()
        self.assertEqual(len(result.entries), 1)
        self.assertEqual(result.duplicates, 1)

    def test_entry_ids_are_a_function_of_the_run_not_the_file(self) -> None:
        plan = catalog.plan_key(_PLAN)
        run = catalog.run_key(plan, "run-1")
        self.write_events([_started("run-1", at=10), _finished("run-1", at=20)])
        self.assertEqual(
            self.run_import().entries[0].entry_id, history.entry_id_for(plan, run)
        )
