# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405
"""ASL assurance vocabulary, fingerprint, evidence, and bounds tests."""

# ruff: noqa: F403, F405
from tests.asl_assurance_models_support import *


class VocabularyTests(unittest.TestCase):
    def test_rubric_covers_every_required_lane(self) -> None:
        self.assertEqual(
            (
                "identity-authority",
                "task-packet-coherence",
                "lifecycle-retry-resume",
                "verification-review-independence",
                "mutation-commit-guards",
                "truthfulness-observability",
                "resources-stalls",
                "unknown-anomalies",
            ),
            assurance_models_vocab.RUBRIC_LANES,
        )

    def test_audit_states_match_the_persisted_vocabulary(self) -> None:
        self.assertEqual(
            ("all-clear", "findings", "partial", "unavailable", "skipped", "pending"),
            assurance_models_vocab.AUDIT_STATES,
        )

    def test_severities_are_ordered_low_to_critical(self) -> None:
        self.assertEqual(
            ("low", "medium", "high", "critical"), assurance_models_vocab.SEVERITIES
        )

    def test_every_disposition_names_the_state_it_produces(self) -> None:
        self.assertEqual(
            set(assurance_models_vocab.DISPOSITION_KINDS),
            set(assurance_models_vocab.DISPOSITION_RESULT_STATE),
        )
        for state in assurance_models_vocab.DISPOSITION_RESULT_STATE.values():
            self.assertIn(state, assurance_models_vocab.ISSUE_STATES)


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_ignores_run_time_and_narrative_fields(self) -> None:
        baseline = _finding()
        volatile = _finding(
            severity="low",
            summary="Different words for the same defect.",
            recommendation="Different advice.",
            evidence=("state.json", "events.jsonl"),
        )

        self.assertEqual(baseline.fingerprint, volatile.fingerprint)

    def test_each_identity_field_changes_the_fingerprint(self) -> None:
        baseline = _finding().fingerprint

        self.assertNotEqual(baseline, _finding(category="mutation").fingerprint)
        self.assertNotEqual(baseline, _finding(component="verification").fingerprint)
        self.assertNotEqual(
            baseline, _finding(invariant_key="retry.budget-exceeded").fingerprint
        )

    def test_the_schema_version_participates_in_the_digest(self) -> None:
        material = "\x1f".join(
            (
                "assurance-finding",
                str(assurance_models_vocab.ASSURANCE_SCHEMA_VERSION),
                "lifecycle",
                "agent_session_loop/runner.py",
                "retry.budget-respected",
            )
        )

        self.assertEqual(
            hashlib.sha256(material.encode("utf-8")).hexdigest(),
            _finding().fingerprint,
        )

    def test_fingerprint_takes_only_the_four_identity_inputs(self) -> None:
        # The signature is the guarantee: there is no parameter through which a
        # run id, timestamp, or summary could reach the digest.
        with self.assertRaises(TypeError):
            assurance_models_findings.finding_fingerprint(  # type: ignore[call-arg]
                schema_version=assurance_models_vocab.ASSURANCE_SCHEMA_VERSION,
                category="lifecycle",
                component="runner",
                invariant_key="retry.budget-respected",
                run_id=RUN_ID,
            )

    def test_unknown_schema_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "unsupported"):
            assurance_models_findings.finding_fingerprint(
                schema_version=99,
                category="lifecycle",
                component="runner",
                invariant_key="retry.budget-respected",
            )

    def test_declared_fingerprint_must_match_the_derived_one(self) -> None:
        payload = _finding().to_dict()
        payload["fingerprint"] = "c" * 64

        with self.assertRaisesRegex(AssuranceError, "does not match"):
            assurance_models_findings.parse_finding(payload)


class AllClearTests(unittest.TestCase):
    def test_all_clear_needs_every_lane(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "every rubric lane"):
            _audit(lanes=_lanes()[:-1])

    def test_all_clear_needs_every_lane_to_pass(self) -> None:
        lanes = list(_lanes())
        lanes[2] = assurance_models_findings.LaneEvidence(
            lane=lanes[2].lane,
            status="unavailable",
            summary="The event ledger was truncated.",
            evidence=("events.jsonl",),
        )

        with self.assertRaisesRegex(AssuranceError, "all-clear requires every"):
            _audit(lanes=tuple(lanes))

    def test_all_clear_cannot_carry_findings(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "cannot carry findings"):
            _audit(findings=(_finding(),))

    def test_findings_state_needs_at_least_one_finding(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at least one finding"):
            _audit(state="findings")

    def test_findings_state_still_needs_full_lane_coverage(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "every rubric lane"):
            _audit(state="findings", findings=(_finding(),), lanes=_lanes()[:3])

    def test_duplicate_lanes_are_rejected(self) -> None:
        lanes = _lanes()

        with self.assertRaisesRegex(AssuranceError, "repeats rubric lane"):
            _audit(lanes=lanes + (lanes[0],))

    def test_unavailable_audit_keeps_partial_lanes_and_names_a_reason(self) -> None:
        audit = _audit(
            state="unavailable",
            reason="agent-transport-failure",
            lanes=_lanes()[:2],
        )

        self.assertEqual("unavailable", audit.state)
        self.assertEqual(2, len(audit.lanes))

    def test_unavailable_audit_must_say_why(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must record why"):
            _audit(state="unavailable", lanes=_lanes()[:2])

    def test_skipped_audit_performs_no_analysis(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "no lanes, findings"):
            _audit(state="skipped", reason="operator-opted-out")

    def test_clean_states_must_not_invent_a_reason(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must not record a reason"):
            _audit(reason="operator-opted-out")


class EvidenceReferenceTests(unittest.TestCase):
    def test_traversal_and_absolute_locators_are_rejected(self) -> None:
        for locator in (
            "../../etc/passwd",
            "/etc/passwd",
            "C:\\Windows\\system32",
            "~/secrets",
            "turns/../../escape.json",
            "./events.jsonl",
        ):
            with (
                self.subTest(locator=locator),
                self.assertRaises(AssuranceError),
            ):
                assurance_models_findings.EvidenceRef(kind="artifact", locator=locator)

    def test_a_reference_to_undeclared_evidence_is_rejected(self) -> None:
        lanes = list(_lanes())
        lanes[0] = assurance_models_findings.LaneEvidence(
            lane=lanes[0].lane,
            status="pass",
            summary="Judged from an artifact the audit never declared.",
            evidence=("turns/1/never-declared.json",),
        )

        with self.assertRaisesRegex(AssuranceError, "never declared"):
            _audit(lanes=tuple(lanes))

    def test_a_lane_must_name_evidence(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at least one evidence locator"):
            assurance_models_findings.LaneEvidence(
                lane="resources-stalls",
                status="pass",
                summary="Nothing to see here.",
                evidence=(),
            )

    def test_duplicate_evidence_locators_are_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "repeats locator"):
            _audit(
                evidence=_evidence()
                + (
                    assurance_models_findings.EvidenceRef(
                        kind="event", locator="events.jsonl"
                    ),
                )
            )

    def test_a_malformed_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "SHA-256 hex digest"):
            assurance_models_findings.EvidenceRef(
                kind="artifact", locator="report.json", digest="beef"
            )


class BoundsTests(unittest.TestCase):
    def test_oversized_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at most"):
            _finding(summary="x" * (assurance_models_vocab.MAX_TEXT_CHARS + 1))

    def test_oversized_reference_lists_are_rejected(self) -> None:
        locators = tuple(
            f"turns/{index}/log.txt"
            for index in range(assurance_models_vocab.MAX_REFERENCES_PER_ITEM + 1)
        )

        with self.assertRaisesRegex(AssuranceError, "at most"):
            _finding(evidence=locators)

    def test_padded_text_is_rejected_rather_than_trimmed(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "leading or trailing whitespace"):
            _finding(summary="  A retry ran after the budget was exhausted.\n")

    def test_text_with_a_lone_surrogate_is_rejected(self) -> None:
        # UTF-8 cannot encode it, so the ledger write or a print would raise.
        with self.assertRaisesRegex(AssuranceError, "lone surrogate"):
            _finding(summary="A retry ran \ud800 after the budget.")

    def test_malformed_severity_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'severity' must be one of"):
            _finding(severity="catastrophic")

    def test_malformed_lane_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'lane' must be one of"):
            _finding(lane="vibes")

    def test_unknown_keys_are_rejected(self) -> None:
        payload = _audit().to_dict()
        payload["extra"] = True

        with self.assertRaisesRegex(AssuranceError, "unknown key"):
            assurance_models_audit.parse_supervisor_audit(payload)

    def test_unknown_schema_version_is_rejected_before_anything_else(self) -> None:
        payload = _audit().to_dict()
        payload["schema_version"] = 99

        with self.assertRaisesRegex(AssuranceError, "unsupported"):
            assurance_models_audit.parse_supervisor_audit(payload)

    def test_duplicate_findings_in_one_audit_are_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "repeats fingerprint"):
            _audit(state="findings", findings=(_finding(), _finding(severity="low")))
