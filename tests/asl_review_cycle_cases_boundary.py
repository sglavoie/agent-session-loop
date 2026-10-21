# mypy: ignore-errors
from .asl_review_cycle_support import *
from .asl_review_cycle_support import _call_receivers, _resume


class ReviewCycleBoundaryTests(unittest.TestCase):
    """The structural contract of the boundary itself."""

    @property
    def source(self) -> str:
        return Path(review_cycle.__file__ or "").read_text(encoding="utf-8")

    def test_the_module_does_not_import_the_runner(self) -> None:
        assert_no_runner_import(self, review_cycle)

    def test_the_runner_keeps_no_alias_for_the_moved_symbols(self) -> None:
        for name in MOVED_SYMBOLS:
            with self.subTest(name=name):
                self.assertFalse(hasattr(runner, name))
                self.assertTrue(hasattr(review_cycle, name))

    def test_nothing_here_opens_a_file_of_its_own(self) -> None:
        # The plan directory is mutated on one path only, and through
        # `review_repair`/`readiness_gate` rather than directly. Any file this
        # module learned to open would be a durable write outside the lock that
        # serializes every other record the run keeps.
        called = called_names(ast.parse(self.source))
        for reach in (
            "open",
            "write_text",
            "write_bytes",
            "mkdir",
            "unlink",
            "replace",
            "fullsync",
        ):
            self.assertNotIn(reach, called, msg=reach)
        self.assertNotIn("durability.", self.source)

    def test_every_state_and_event_write_goes_through_a_persistence_handle(
        self,
    ) -> None:
        self.assertEqual(
            {"persistence"},
            _call_receivers(self.source, "append_event", "write_state"),
        )

    def test_the_runner_verdict_reader_still_satisfies_the_seam(self) -> None:
        """``ReviewVerdictReader`` is structural, so drift here is silent.

        Nothing raises when a function passed as a plain callable stops
        satisfying a Protocol. Attestation is the one place a review the run
        already refused could be re-projected as if it stood, so a reader that
        quietly stopped honouring ``require_artifact`` would publish a verdict
        on evidence the caller believed it had demanded.
        """
        declared = inspect.signature(review_delta.ReviewVerdictReader.__call__)
        actual = inspect.signature(handoff_guards._read_review_verdict)

        expected = [
            parameter
            for name, parameter in declared.parameters.items()
            if name != "self"
        ]
        self.assertEqual(
            [(p.name, p.kind, p.default) for p in expected],
            [(p.name, p.kind, p.default) for p in actual.parameters.values()],
        )

    def test_the_bound_reinspector_still_satisfies_the_seam(self) -> None:
        """The post-mutation re-read is a callback, not an import.

        Session 23 gave ``_reinspect_after_plan_mutation`` a home in
        ``session_verification``, but it stays a seam here: importing it would
        point this boundary at the module that verifies the turns it reopens. A
        positional-shape change there would be caught by mypy at the one call
        site, but the *meaning* would not: the cache comes first and the
        fallback second, and swapping them would still type-check nowhere near
        here.
        """
        actual = inspect.signature(session_verification._reinspect_after_plan_mutation)
        self.assertEqual(["plan_cache", "fallback"], list(actual.parameters))
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                for parameter in actual.parameters.values()
            )
        )


class PriorReviewCapTests(unittest.TestCase):
    """A missing cap is not a zero cap."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"

    def record_cycle(self, **fields: Any) -> None:
        record: dict[str, Any] = {
            "cycle": 1,
            "review_session_id": "2",
            "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        }
        record.update(fields)
        events.append_event(
            self.event_path, events.Event(events.REVIEW_CYCLE_EVENT, record)
        )

    def test_a_legacy_cycle_without_the_field_reports_no_cap(self) -> None:
        # Events written before the cap was recorded are indistinguishable from
        # a cycle that chose zero if the absence is flattened to a number, and
        # a cap of zero reads as a deliberate refusal to repair.
        self.record_cycle()
        self.assertIsNone(review_cycle._prior_review_cap(self.event_path, _resume()))

    def test_no_history_at_all_reports_no_cap(self) -> None:
        self.assertIsNone(review_cycle._prior_review_cap(self.event_path, _resume()))

    def test_the_source_cycles_own_cap_is_the_one_reported(self) -> None:
        self.record_cycle(cycle=1, review_session_id="2", max_review_fix_cycles=1)
        self.record_cycle(cycle=2, review_session_id="4", max_review_fix_cycles=5)
        self.assertEqual(
            1,
            review_cycle._prior_review_cap(
                self.event_path, _resume(source_cycle=1, review_session_id="2")
            ),
        )

    def test_a_cap_that_is_not_a_whole_count_is_not_a_cap(self) -> None:
        # `True` is an int in Python and a cap of 1 is a real budget, so the
        # narrow type check is the only thing standing between a corrupt record
        # and a repair budget nobody chose.
        for value in (True, -1, "2", 2.0, None):
            with self.subTest(value=value):
                self.event_path.unlink(missing_ok=True)
                self.record_cycle(max_review_fix_cycles=value)
                self.assertIsNone(
                    review_cycle._prior_review_cap(self.event_path, _resume())
                )


class ReopenLineageTests(unittest.TestCase):
    """What an operator reads to find out why a repair never appeared."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"

    def record(self, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "event_path": self.event_path,
            "payload": {"run_id": "run-1"},
            "resume": _resume(),
            "max_review_fix_cycles": 3,
            "materialized": True,
            "result": "materialized",
        }
        fields.update(overrides)
        return review_cycle._review_reopen_record(**fields)

    def test_a_successful_reopen_advances_the_materialization_count(self) -> None:
        record = self.record()
        self.assertEqual(1, record["historical_materializations_before"])
        self.assertEqual(2, record["historical_materializations_after"])

    def test_a_refused_reopen_is_recorded_without_advancing_the_count(self) -> None:
        record = self.record(
            materialized=False, result="refused", error="higher-cap-required"
        )
        self.assertEqual(1, record["historical_materializations_before"])
        self.assertEqual(1, record["historical_materializations_after"])
        self.assertEqual("higher-cap-required", record["error"])

    def test_a_recovered_backfill_counts_its_source_once(self) -> None:
        """The crash-window record describes a materialization already counted.

        Children written before the crash are part of the count the evaluator
        reconciled, so a backfill that took `historical_materializations` at
        face value would report an "after" one higher than the number of times
        repairs were actually opened — forever, because the lineage is
        append-only.
        """
        record = self.record(
            recovered=True, resume=_resume(historical_materializations=1)
        )
        self.assertEqual(0, record["historical_materializations_before"])
        self.assertEqual(1, record["historical_materializations_after"])

    def test_a_recovered_backfill_never_reports_a_negative_count(self) -> None:
        record = self.record(
            recovered=True, resume=_resume(historical_materializations=0)
        )
        self.assertEqual(0, record["historical_materializations_before"])
        self.assertEqual(1, record["historical_materializations_after"])

    def test_the_record_names_the_cap_on_both_sides_of_the_reopen(self) -> None:
        events.append_event(
            self.event_path,
            events.Event(
                events.REVIEW_CYCLE_EVENT,
                {"cycle": 1, "review_session_id": "2", "max_review_fix_cycles": 1},
            ),
        )
        record = self.record(max_review_fix_cycles=3)
        self.assertEqual(1, record["before_max_review_fix_cycles"])
        self.assertEqual(3, record["after_max_review_fix_cycles"])

    def test_the_attempt_lands_in_state_and_the_event_log_together(self) -> None:
        payload: dict[str, Any] = {"run_id": "run-1"}
        record = self.record(payload=payload)
        review_cycle._record_review_reopen(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            record=record,
        )
        self.assertEqual(record, payload["review_reopen"])
        appended = [
            entry
            for entry in read_jsonl(self.event_path)
            if entry.get("name") == events.REVIEW_REPAIR_REOPENED_EVENT
        ]
        self.assertEqual(1, len(appended))

    def test_durable_success_is_only_claimed_for_the_named_source(self) -> None:
        payload: dict[str, Any] = {"run_id": "run-1"}
        review_cycle._record_review_reopen(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            record=self.record(materialized=False, result="refused"),
        )
        # A refused attempt for the same source is not success, and an unnamed
        # source cannot inherit another session's.
        self.assertFalse(
            review_cycle._has_successful_review_reopen(self.event_path, "2")
        )
        self.assertFalse(
            review_cycle._has_successful_review_reopen(self.event_path, None)
        )

        review_cycle._record_review_reopen(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            record=self.record(),
        )
        self.assertTrue(
            review_cycle._has_successful_review_reopen(self.event_path, "2")
        )
        self.assertFalse(
            review_cycle._has_successful_review_reopen(self.event_path, "4")
        )
