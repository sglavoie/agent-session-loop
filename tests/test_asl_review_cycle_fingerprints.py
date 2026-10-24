"""Review finding fingerprint, cap and baseline tests."""

import json
import unittest

from agent_session_loop import (
    events,
    review,
    review_continuation,
    review_delta,
    review_guard,
    run_inputs,
    runner,
)
from tests.review_cycle_support import RepairCycleFixtures
from tests.support import (
    _structured_review_finding,
)

ASL_TEST_LANE = "integration"


class ReviewCycleFingerprintTests(RepairCycleFixtures, unittest.TestCase):
    """Fingerprint review findings and resolve cap inputs."""

    def test_finding_fingerprint_ignores_line_number_drift(self) -> None:
        original = _structured_review_finding(
            title="Unbounded retry loop at runner.py:100",
            evidence=[
                {
                    "path": "agent_session_loop/runner.py:100",
                    "symbol": "supervise_run",
                }
            ],
        )
        drifted = _structured_review_finding(
            finding_id="F1-redetected",
            title="Unbounded retry loop at runner.py:250",
            evidence=[
                {
                    "path": "agent_session_loop/runner.py:250:8",
                    "symbol": "supervise_run",
                }
            ],
            explanation="Re-detected on the second review.",
        )

        self.assertEqual(
            review.finding_fingerprint(original),
            review.finding_fingerprint(drifted),
        )
        self.assertTrue(review.finding_fingerprint(original).startswith("finding:"))

    def test_finding_fingerprint_pins_legacy_identity_and_scopes_repository(
        self,
    ) -> None:
        legacy = _structured_review_finding(
            finding_id="f1",
            severity="high",
            title="Broken thing at line 12",
            evidence=[{"path": "a/b.py:12", "symbol": "do_thing"}],
        )

        self.assertEqual("finding:a8b33340696c4f61", review.finding_fingerprint(legacy))
        svc = dict(legacy, repo="SVC")
        oak = dict(legacy, repo="OAK")
        self.assertNotEqual(
            review.finding_fingerprint(svc), review.finding_fingerprint(oak)
        )

    def test_finding_fingerprint_normalizes_title_and_evidence_order(
        self,
    ) -> None:
        one = _structured_review_finding(
            title="  Retry Loop   is unbounded (see lines 12-34)",
            evidence=[
                {"path": "pkg/a.py", "symbol": "alpha"},
                {"path": "pkg/b.py:9"},
            ],
        )
        two = _structured_review_finding(
            title="retry loop is unbounded (see line 99)",
            evidence=[
                {"path": "pkg/b.py"},
                {"path": "pkg/a.py:44", "symbol": "alpha"},
            ],
        )

        self.assertEqual(
            review.finding_fingerprint(one), review.finding_fingerprint(two)
        )

    def test_finding_fingerprint_changes_with_identity_fields(self) -> None:
        base = _structured_review_finding()
        for overrides in (
            {"severity": "low"},
            {"title": "A different defect"},
            {"evidence": [{"path": "pkg/other.py", "symbol": "run_loop"}]},
            {
                "evidence": [
                    {
                        "path": "agent_session_loop/runner.py",
                        "symbol": "other_symbol",
                    }
                ]
            },
        ):
            changed = _structured_review_finding(**overrides)
            self.assertNotEqual(
                review.finding_fingerprint(base),
                review.finding_fingerprint(changed),
                overrides,
            )

    def test_finding_fingerprints_handles_missing_findings(self) -> None:
        self.assertIsNone(review.finding_fingerprints(None))
        self.assertEqual((), review.finding_fingerprints([]))
        fingerprints = review.finding_fingerprints([_structured_review_finding()])
        self.assertEqual(1, len(fingerprints))

    def test_max_review_fix_cycles_resolution_and_alias(self) -> None:
        self.assertEqual(
            run_inputs.DEFAULT_MAX_REVIEW_FIX_CYCLES,
            run_inputs._max_review_fix_cycles(self._run_args()),
        )
        self.assertEqual(
            0,
            run_inputs._max_review_fix_cycles(self._run_args(max_review_fix_cycles=0)),
        )
        self.assertEqual(
            5,
            run_inputs._max_review_fix_cycles(self._run_args(max_review_fix_plans=5)),
        )
        self.assertEqual(
            1,
            run_inputs._max_review_fix_cycles(
                self._run_args(max_review_fix_cycles=1, max_review_fix_plans=9)
            ),
        )

    def test_run_input_validation_for_review_fix_flags(self) -> None:
        run_inputs._validate_run_inputs(
            self._run_args(max_review_fix_cycles=0),
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
        run_inputs._validate_run_inputs(
            self._run_args(max_review_fix_plans=0),
            resolve_review_guard_excludes=review_guard._review_guard_excludes,
        )
        with self.assertRaisesRegex(
            runner.RunError, "--max-review-fix-cycles must be zero"
        ):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_cycles=-1),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )
        with self.assertRaisesRegex(
            runner.RunError, "--max-review-fix-plans must be zero"
        ):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_plans=-2),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )
        with self.assertRaisesRegex(runner.RunError, "deprecated alias"):
            run_inputs._validate_run_inputs(
                self._run_args(max_review_fix_cycles=2, max_review_fix_plans=2),
                resolve_review_guard_excludes=review_guard._review_guard_excludes,
            )

    def test_review_cycle_state_reads_prior_events(self) -> None:
        event_path = self.tmp_path / "events.jsonl"
        self.assertEqual(
            {
                "cycle": 1,
                "repairs_materialized": 0,
                "previous_fingerprints": (),
                "previous_repeat_keys": (),
            },
            review_delta._review_cycle_state(event_path),
        )
        records = [
            {
                "name": events.REVIEW_CYCLE_EVENT,
                "payload": {
                    "cycle": 1,
                    "disposition": review_continuation.REVIEW_CYCLE_DISPOSITION_REPAIRS,
                    "fingerprints": ["finding:aaaa", "finding:bbbb"],
                },
            },
            {"name": "turn_finished", "payload": {}},
            {
                "name": events.REVIEW_CYCLE_EVENT,
                "payload": {
                    "cycle": 2,
                    "disposition": (
                        review_continuation.REVIEW_CYCLE_DISPOSITION_ALL_CLEAR
                    ),
                    "fingerprints": [],
                },
            },
        ]
        event_path.write_text(
            "\n".join(json.dumps(record) for record in records) + "\n",
            encoding="utf-8",
        )

        state_payload = review_delta._review_cycle_state(event_path)
        self.assertEqual(3, state_payload["cycle"])
        self.assertEqual(1, state_payload["repairs_materialized"])
        self.assertEqual((), state_payload["previous_fingerprints"])

    def test_repeat_survives_a_moved_evidence_path_within_the_same_group(self) -> None:
        original = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Review repeat detection ignores drifted evidence",
            evidence=[{"path": "agent_session_loop/runner.py:10625", "symbol": "run"}],
        )
        moved = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Review repeat detection ignores drifted evidence",
            evidence=[{"path": "agent_session_loop/review.py:42", "symbol": "run"}],
        )
        unrelated = _structured_review_finding(
            finding_id="F2",
            repair_group="preflight",
            title="A different defect entirely",
            evidence=[{"path": "agent_session_loop/preflight.py:1", "symbol": "run"}],
        )

        self.assertNotEqual(
            review.finding_fingerprint(original), review.finding_fingerprint(moved)
        )
        self.assertEqual(
            review.finding_repeat_key(original), review.finding_repeat_key(moved)
        )
        self.assertNotEqual(
            review.finding_repeat_key(original), review.finding_repeat_key(unrelated)
        )

        repeated = review_continuation.repeated_finding_positions(
            fingerprints=review.finding_fingerprints([moved, unrelated]),
            repeat_keys=review.finding_repeat_keys([moved, unrelated]),
            previous_fingerprints=review.finding_fingerprints([original]),
            previous_repeat_keys=review.finding_repeat_keys([original]),
        )

        self.assertEqual((0,), repeated)

    def test_repeat_key_separates_the_same_defect_in_a_different_group(self) -> None:
        docs = _structured_review_finding(
            finding_id="F1",
            repair_group="docs",
            title="Same title, different group",
            evidence=[{"path": "README.md", "symbol": "x"}],
        )
        runner_group = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="Same title, different group",
            evidence=[{"path": "README.md", "symbol": "x"}],
        )

        self.assertNotEqual(
            review.finding_repeat_key(docs), review.finding_repeat_key(runner_group)
        )
        self.assertEqual(
            (),
            review_continuation.repeated_finding_positions(
                fingerprints=review.finding_fingerprints([runner_group]),
                repeat_keys=review.finding_repeat_keys([runner_group]),
                previous_fingerprints=(),
                previous_repeat_keys=review.finding_repeat_keys([docs]),
            ),
        )

    def test_reopen_baseline_recovers_repeat_keys_from_stored_findings(self) -> None:
        finding = _structured_review_finding(
            finding_id="F1",
            repair_group="runner",
            title="A defect reopened before repeat keys were recorded",
            evidence=[{"path": "agent_session_loop/runner.py:1", "symbol": "run"}],
        )
        event_path = self.tmp_path / "legacy-reopen-keys.jsonl"
        event_path.write_text(
            json.dumps(
                {
                    "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                    "payload": {
                        "source_cycle": 1,
                        "materialized": True,
                        "current_fingerprints": [review.finding_fingerprint(finding)],
                        "current_findings": [finding],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        state_payload = review_delta._review_cycle_state(event_path)

        self.assertEqual(
            review.finding_repeat_keys([finding]),
            state_payload["previous_repeat_keys"],
        )

    def test_post_reopen_baseline_uses_ordered_source_lineage(self) -> None:
        cases = (
            ("same-source", "3", True, ("finding:same",)),
            ("newer-source", "4", True, ("finding:newer",)),
            ("older-source", "2", True, ("finding:cycle",)),
            ("failed-newer", "4", False, ("finding:cycle",)),
            ("malformed-source", "not-a-session", True, ("finding:cycle",)),
        )
        for name, reopen_source, materialized, expected in cases:
            with self.subTest(name=name):
                event_path = self.tmp_path / f"{name}.jsonl"
                event_path.write_text(
                    "".join(
                        json.dumps(record) + "\n"
                        for record in (
                            {
                                "name": events.REVIEW_CYCLE_EVENT,
                                "payload": {
                                    "cycle": 2,
                                    "review_session_id": "3",
                                    "fingerprints": ["finding:cycle"],
                                },
                            },
                            {
                                "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                                "payload": {
                                    "source_cycle": 99,
                                    "source_review_session_id": reopen_source,
                                    "materialized": materialized,
                                    "current_fingerprints": [
                                        f"finding:{name.split('-')[0]}"
                                    ],
                                },
                            },
                        )
                    ),
                    encoding="utf-8",
                )

                state_payload = review_delta._review_cycle_state(event_path)

                self.assertEqual(expected, state_payload["previous_fingerprints"])

        legacy_path = self.tmp_path / "legacy-cycle-fallback.jsonl"
        legacy_path.write_text(
            "".join(
                json.dumps(record) + "\n"
                for record in (
                    {
                        "name": events.REVIEW_CYCLE_EVENT,
                        "payload": {
                            "cycle": 2,
                            "fingerprints": ["finding:legacy"],
                        },
                    },
                    {
                        "name": events.REVIEW_REPAIR_REOPENED_EVENT,
                        "payload": {
                            "source_cycle": 2,
                            "materialized": True,
                            "current_fingerprints": ["finding:corrected"],
                        },
                    },
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            ("finding:corrected",),
            review_delta._review_cycle_state(legacy_path)["previous_fingerprints"],
        )


if __name__ == "__main__":
    unittest.main()
