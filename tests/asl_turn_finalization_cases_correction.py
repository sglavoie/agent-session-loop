from .asl_turn_finalization_support import *


class HandoffCorrectionAttemptTests(unittest.TestCase):
    def write_events(self, path: Path, records: list[dict[str, object]]) -> None:
        path.write_text(
            "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
        )

    def correction(self, session_id: str, run_id: str = "run-a") -> dict[str, object]:
        return {
            "name": events.TURN_HANDOFF_CORRECTION_EVENT,
            "payload": {"session_id": session_id, "run_id": run_id},
        }

    def test_missing_event_log_starts_at_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                1,
                handoff_correction._handoff_correction_attempt(
                    Path(tmp) / "none.jsonl", "1", "run-a"
                ),
            )

    def test_prior_corrections_for_the_session_advance_the_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(
                path,
                [
                    self.correction("1"),
                    {"name": "turn_finished", "payload": {"session_id": "1"}},
                    self.correction("1"),
                ],
            )
            self.assertEqual(
                3, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_other_sessions_do_not_consume_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(path, [self.correction("2"), self.correction("3")])
            self.assertEqual(
                1, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_an_earlier_runs_corrections_do_not_consume_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(
                path,
                [
                    self.correction("1", run_id="run-a"),
                    {"name": "run_started", "payload": {"run_id": "run-b"}},
                ],
            )
            self.assertEqual(
                1, handoff_correction._handoff_correction_attempt(path, "1", "run-b")
            )
            self.assertEqual(
                2, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_legacy_session_key_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            self.write_events(
                path,
                [
                    {
                        "name": events.TURN_HANDOFF_CORRECTION_EVENT,
                        "payload": {"session": "1", "run_id": "run-a"},
                    }
                ],
            )
            self.assertEqual(
                2, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_corrupt_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(
                f"not json\n\n{json.dumps(self.correction('1'))}\n[]\n",
                encoding="utf-8",
            )
            self.assertEqual(
                2, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_undecodable_and_over_deep_lines_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(
                b"\xff\xfe not utf-8\n"
                + b"[" * 1_000_000
                + b"\n"
                + json.dumps(self.correction("1")).encode("utf-8")
                + b"\n"
            )
            self.assertEqual(
                2, handoff_correction._handoff_correction_attempt(path, "1", "run-a")
            )

    def test_correction_note_names_the_defect_and_the_repair(self) -> None:
        defect = handoff_correction._HandoffDefect(
            code=handoff_correction.HANDOFF_DEFECT_MISSING_FILES,
            detail="the completed handoff carries no Files modified entry",
            instruction="list every file you changed",
        )
        plain = handoff_correction._handoff_correction_note(
            defect, preserved_review=None
        )
        self.assertIn(defect.detail, plain)
        self.assertIn(defect.instruction, plain)
        self.assertNotIn("review artifacts", plain)
        with_review = handoff_correction._handoff_correction_note(
            defect, preserved_review={"review_verdict_ok": True}
        )
        self.assertIn("review artifacts", with_review)
