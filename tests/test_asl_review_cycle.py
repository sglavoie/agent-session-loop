"""What survives a review verdict, asserted at the boundary rather than a run.

The behavioural coverage of the repair/rereview loop already lives where it
always has — ``tests/test_asl_review.py`` owns the cycle accounting,
``tests/test_asl_review_lifecycle.py`` drives real runs through reopen,
materialization and pause, and ``tests/test_asl_cli.py`` owns the run JSON
those paths publish. What is asserted here is the contract those modules
cannot state, because they exercise the run rather than the boundary:

* the module does not import the runner, and needs nothing from it;
* it opens no file of its own — every durable write goes through the
  ``_RunPersistence`` handle the caller supplied, under that handle's lock;
* both seams the runner still satisfies are structural, so drift in either is
  silent: ``handoff_guards._read_review_verdict`` has to keep matching
  ``review_delta.ReviewVerdictReader`` and ``runner._reinspect_after_plan_mutation``
  has to keep matching :data:`review_cycle.PlanMutationReinspector` until
  Session 23 gives the latter a home; and
* the arithmetic nobody watching a run can see is right — a missing cap is not
  a zero cap, a recovered backfill counts its source once, and a refused reopen
  is recorded as loudly as a successful one.

The last two are why this file exists. A reader whose signature drifted still
type-checks at the one call site that passes it by keyword, and reopen lineage
is only ever read after the fact, by an operator asking why a repair never
appeared — which is exactly when a miscount is no longer reproducible.
"""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import (
    events,
    handoff_guards,
    review_continuation,
    review_cycle,
    review_delta,
    review_transitions,
    run_persistence,
    runner,
)
from tests.support import assert_no_runner_import, read_jsonl

#: The symbols this module took from the runner. The program's no-alias rule
#: says the runner keeps none of them behind, so a re-export added later to
#: rescue a stale patch target fails here rather than in review.
MOVED_SYMBOLS = (
    "_prior_review_cap",
    "_review_reopen_record",
    "_record_review_reopen",
    "_has_successful_review_reopen",
    "_set_review_resume",
    "_terminal_review_reason",
    "_set_existing_review_verdict",
    "_attest_executed_review",
    "_record_review_cycle",
    "_advance_review_cycle",
)


def _call_receivers(source: str, *attributes: str) -> set[str]:
    """Every receiver of a call to one of ``attributes``, as source text."""
    receivers: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in attributes:
            continue
        receivers.add(ast.unparse(node.func.value))
    return receivers


def _called_names(source: str) -> set[str]:
    """Every name this module calls, whether bare or through an attribute.

    Matched on the parse tree rather than the text, because the substring
    ``open(`` also occurs inside ``_record_review_reopen(`` — the kind of false
    positive that gets a real assertion deleted rather than fixed.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _resume(**overrides: Any) -> review_continuation.ReviewResume:
    """A continuation projection with only the fields a caller cares about."""
    fields: dict[str, Any] = {
        "source_cycle": 1,
        "review_session_id": "2",
        "source_disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        "action": "materialize-repairs",
        "eligible": True,
        "blocker": None,
        "historical_materializations": 1,
        "minimum_required_cap": 2,
        "selected_cap": 2,
    }
    fields.update(overrides)
    return review_continuation.ReviewResume(**fields)


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
        called = _called_names(self.source)
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

    def test_the_runner_reinspector_still_satisfies_the_seam(self) -> None:
        """The post-mutation re-read is a callback, not an import.

        ``_reinspect_after_plan_mutation`` does not move until Session 23, so
        until then it is passed in. A positional-shape change there would be
        caught by mypy at the one call site, but the *meaning* would not: the
        cache comes first and the fallback second, and swapping them would
        still type-check nowhere near here.
        """
        actual = inspect.signature(runner._reinspect_after_plan_mutation)
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


class TerminalReviewReasonTests(unittest.TestCase):
    """The reason is the adjudicator's; this boundary only records it."""

    def test_a_cleared_review_leaves_the_payload_alone(self) -> None:
        payload: dict[str, Any] = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
            ),
            "review_verdict": {"outcome": "all-clear", "total_findings": 0},
        }
        reason = review_cycle._terminal_review_reason(payload)
        self.assertEqual(review_transitions.REVIEW_ALL_COMPLETE_REASON, reason)
        self.assertNotIn("ok", payload)
        self.assertNotIn("action_required", payload)

    def test_an_uncleared_review_stops_the_run_claiming_success(self) -> None:
        payload: dict[str, Any] = {
            "review_fix_disposition": (
                review_continuation.REVIEW_CYCLE_DISPOSITION_CAP_EXHAUSTED
            ),
            "review_verdict": {"outcome": "findings", "total_findings": 2},
        }
        reason = review_cycle._terminal_review_reason(payload)
        self.assertEqual(review_transitions.REVIEW_FINDINGS_UNRESOLVED_REASON, reason)
        self.assertIs(False, payload["ok"])
        self.assertEqual(2, payload["action_required"]["unresolved_findings"])


class VerdictAttestationTests(unittest.TestCase):
    """What a review left on disk, projected without the reader moving yet."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan_dir = Path(tmp.name)
        self.calls: list[tuple[Path, bool]] = []

    def reader(self, verdict: dict[str, Any]) -> review_delta.ReviewVerdictReader:
        def read(plan_dir: Path, *, require_artifact: bool = False) -> dict[str, Any]:
            self.calls.append((plan_dir, require_artifact))
            return verdict

        return read

    def test_a_verdict_the_reader_refused_is_not_projected(self) -> None:
        payload: dict[str, Any] = {}
        review_cycle._set_existing_review_verdict(
            payload,
            self.plan_dir,
            read_review_verdict=self.reader({"ok": False, "outcome": "all-clear"}),
        )
        self.assertNotIn("review_verdict", payload)

    def test_an_accepted_verdict_is_projected_field_by_field(self) -> None:
        payload: dict[str, Any] = {}
        review_cycle._set_existing_review_verdict(
            payload,
            self.plan_dir,
            read_review_verdict=self.reader(
                {
                    "ok": True,
                    "status_line": "REVIEW: all-clear",
                    "outcome": "all-clear",
                    "total_findings": 0,
                    "findings_by_severity": {},
                    "unrelated": "dropped",
                }
            ),
        )
        self.assertEqual(
            {
                "status_line",
                "outcome",
                "total_findings",
                "findings_by_severity",
            },
            set(payload["review_verdict"]),
        )

    def test_attestation_mirrors_what_the_run_itself_demanded(self) -> None:
        # `require_artifact` is what keeps a verdict the run refused as invalid
        # from being re-projected here as if it stood.
        payload: dict[str, Any] = {}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=True,
            read_review_verdict=self.reader({"ok": False}),
        )
        self.assertEqual([(self.plan_dir, True)], self.calls)

    def test_digests_are_recorded_even_when_the_verdict_does_not_hold(self) -> None:
        # The digests describe what the session left on disk, which stays
        # honest evidence about an unattestable review.
        payload: dict[str, Any] = {}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=False,
            read_review_verdict=self.reader({"ok": False}),
        )
        self.assertNotIn("review_verdict", payload)
        self.assertIsNone(payload["review_output_sha256"])
        self.assertIsNone(payload["review_artifact_sha256"])

    def test_an_already_attested_verdict_is_not_read_a_second_time(self) -> None:
        payload: dict[str, Any] = {"review_verdict": {"outcome": "findings"}}
        review_cycle._attest_executed_review(
            payload,
            self.plan_dir,
            require_artifact=False,
            read_review_verdict=self.reader({"ok": True, "outcome": "all-clear"}),
        )
        self.assertEqual([], self.calls)
        self.assertEqual({"outcome": "findings"}, payload["review_verdict"])

    def test_the_recorder_factory_binds_the_reader_into_the_two_argument_seam(
        self,
    ) -> None:
        """``ExistingReviewVerdictRecorder`` has no room for a reader argument.

        The stop-before-review-work path takes a two-positional callable, so
        the binding happens once here rather than at every stop that reports
        the verdict it already has.
        """
        record = review_cycle.existing_review_verdict_recorder(
            self.reader({"ok": True, "outcome": "all-clear", "total_findings": 0})
        )
        payload: dict[str, Any] = {}
        record(payload, self.plan_dir)
        self.assertEqual([(self.plan_dir, False)], self.calls)
        self.assertEqual("all-clear", payload["review_verdict"]["outcome"])


class RecordReviewCycleTests(unittest.TestCase):
    """One record, two surfaces, one lock."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.event_path = Path(tmp.name) / "events.jsonl"

    def test_the_disposition_reaches_the_record_the_state_and_the_event(self) -> None:
        payload: dict[str, Any] = {}
        cycle_record: dict[str, Any] = {"run_id": "run-1", "cycle": 1}
        review_cycle._record_review_cycle(
            event_path=self.event_path,
            payload=payload,
            persistence=run_persistence._RunPersistence(write_state=False),
            cycle_record=cycle_record,
            disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
        )
        disposition = review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS
        self.assertEqual(disposition, cycle_record["disposition"])
        self.assertEqual(disposition, payload["review_fix_disposition"])
        self.assertEqual([cycle_record], payload["review_cycles"])
        appended = [
            entry
            for entry in read_jsonl(self.event_path)
            if entry.get("name") == events.REVIEW_CYCLE_EVENT
        ]
        self.assertEqual(1, len(appended))
        self.assertEqual(disposition, appended[0]["payload"]["disposition"])

    def test_cycles_accumulate_rather_than_replace(self) -> None:
        payload: dict[str, Any] = {}
        for cycle in (1, 2):
            review_cycle._record_review_cycle(
                event_path=self.event_path,
                payload=payload,
                persistence=run_persistence._RunPersistence(write_state=False),
                cycle_record={"cycle": cycle},
                disposition=review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
            )
        self.assertEqual(
            [1, 2], [record["cycle"] for record in payload["review_cycles"]]
        )
        self.assertTrue(json.dumps(payload["review_cycles"]))


if __name__ == "__main__":
    unittest.main()
