"""Assurance domain schema, fingerprint, and artifact-writer tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import assurance_models as models
from agent_session_loop.assurance_models import AssuranceError

RUN_ID = "20260810T101112123456Z-4242"
TIMESTAMP = "2026-08-10T10:11:12+00:00"
DIGEST = "a" * 64


def _evidence() -> tuple[models.EvidenceRef, ...]:
    return (
        models.EvidenceRef(kind="event", locator="events.jsonl"),
        models.EvidenceRef(kind="state", locator="state.json", digest=DIGEST),
        models.EvidenceRef(
            kind="task-packet", locator="turns/1/task-packet.json", detail="turn 1"
        ),
    )


def _lanes(status: str = "pass") -> tuple[models.LaneEvidence, ...]:
    return tuple(
        models.LaneEvidence(
            lane=lane,
            status=status,
            summary=f"{lane} judged from the event ledger",
            evidence=("events.jsonl",),
        )
        for lane in models.RUBRIC_LANES
    )


def _finding(**overrides) -> models.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
    }
    payload.update(overrides)
    return models.Finding(**payload)


def _audit(**overrides) -> models.SupervisorAudit:
    payload = {
        "run_id": RUN_ID,
        "state": "all-clear",
        "input_digest": "b" * 64,
        "generated_at": TIMESTAMP,
        "evidence": _evidence(),
        "lanes": _lanes(),
    }
    payload.update(overrides)
    return models.SupervisorAudit(**payload)


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
            models.RUBRIC_LANES,
        )

    def test_audit_states_match_the_persisted_vocabulary(self) -> None:
        self.assertEqual(
            ("all-clear", "findings", "unavailable", "skipped", "pending"),
            models.AUDIT_STATES,
        )

    def test_severities_are_ordered_low_to_critical(self) -> None:
        self.assertEqual(("low", "medium", "high", "critical"), models.SEVERITIES)

    def test_every_disposition_names_the_state_it_produces(self) -> None:
        self.assertEqual(
            set(models.DISPOSITION_KINDS), set(models.DISPOSITION_RESULT_STATE)
        )
        for state in models.DISPOSITION_RESULT_STATE.values():
            self.assertIn(state, models.ISSUE_STATES)


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
                str(models.ASSURANCE_SCHEMA_VERSION),
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
            models.finding_fingerprint(  # type: ignore[call-arg]
                schema_version=models.ASSURANCE_SCHEMA_VERSION,
                category="lifecycle",
                component="runner",
                invariant_key="retry.budget-respected",
                run_id=RUN_ID,
            )

    def test_unknown_schema_version_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "unsupported"):
            models.finding_fingerprint(
                schema_version=99,
                category="lifecycle",
                component="runner",
                invariant_key="retry.budget-respected",
            )

    def test_declared_fingerprint_must_match_the_derived_one(self) -> None:
        payload = _finding().to_dict()
        payload["fingerprint"] = "c" * 64

        with self.assertRaisesRegex(AssuranceError, "does not match"):
            models.parse_finding(payload)


class AllClearTests(unittest.TestCase):
    def test_all_clear_needs_every_lane(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "every rubric lane"):
            _audit(lanes=_lanes()[:-1])

    def test_all_clear_needs_every_lane_to_pass(self) -> None:
        lanes = list(_lanes())
        lanes[2] = models.LaneEvidence(
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
                models.EvidenceRef(kind="artifact", locator=locator)

    def test_a_reference_to_undeclared_evidence_is_rejected(self) -> None:
        lanes = list(_lanes())
        lanes[0] = models.LaneEvidence(
            lane=lanes[0].lane,
            status="pass",
            summary="Judged from an artifact the audit never declared.",
            evidence=("turns/1/never-declared.json",),
        )

        with self.assertRaisesRegex(AssuranceError, "never declared"):
            _audit(lanes=tuple(lanes))

    def test_a_lane_must_name_evidence(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at least one evidence locator"):
            models.LaneEvidence(
                lane="resources-stalls",
                status="pass",
                summary="Nothing to see here.",
                evidence=(),
            )

    def test_duplicate_evidence_locators_are_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "repeats locator"):
            _audit(
                evidence=_evidence()
                + (models.EvidenceRef(kind="event", locator="events.jsonl"),)
            )

    def test_a_malformed_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "SHA-256 hex digest"):
            models.EvidenceRef(kind="artifact", locator="report.json", digest="beef")


class BoundsTests(unittest.TestCase):
    def test_oversized_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at most"):
            _finding(summary="x" * (models.MAX_TEXT_CHARS + 1))

    def test_oversized_reference_lists_are_rejected(self) -> None:
        locators = tuple(
            f"turns/{index}/log.txt"
            for index in range(models.MAX_REFERENCES_PER_ITEM + 1)
        )

        with self.assertRaisesRegex(AssuranceError, "at most"):
            _finding(evidence=locators)

    def test_padded_text_is_rejected_rather_than_trimmed(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "leading or trailing whitespace"):
            _finding(summary="  A retry ran after the budget was exhausted.\n")

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
            models.parse_supervisor_audit(payload)

    def test_unknown_schema_version_is_rejected_before_anything_else(self) -> None:
        payload = _audit().to_dict()
        payload["schema_version"] = 99

        with self.assertRaisesRegex(AssuranceError, "unsupported"):
            models.parse_supervisor_audit(payload)

    def test_duplicate_findings_in_one_audit_are_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "repeats fingerprint"):
            _audit(state="findings", findings=(_finding(), _finding(severity="low")))


class RoundTripTests(unittest.TestCase):
    def _assert_round_trips(self, model, parse) -> None:
        payload = model.to_dict()
        reparsed = parse(payload)

        self.assertEqual(model, reparsed)
        self.assertEqual(
            models.canonical_json_bytes(payload),
            models.canonical_json_bytes(reparsed.to_dict()),
        )

    def test_audit_round_trips(self) -> None:
        audit = _audit(
            state="findings",
            findings=(_finding(),),
            deterministic=(
                models.DeterministicResult(
                    check="state-event-agreement",
                    lane="truthfulness-observability",
                    status="fail",
                    invariant_key="state.matches-events",
                    evidence=("state.json", "events.jsonl"),
                    detail="state says complete; events say paused",
                ),
            ),
        )

        self._assert_round_trips(audit, models.parse_supervisor_audit)

    def test_summary_round_trips(self) -> None:
        summary = models.AssuranceSummary(
            run_id=RUN_ID,
            state="findings",
            eligible=True,
            role=models.AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="review",
                timeout_seconds=600.0,
            ),
            attempts=(
                models.AuditAttempt(number=1, outcome="malformed-output"),
                models.AuditAttempt(
                    number=2,
                    outcome="ok",
                    duration_seconds=42.5,
                    artifact_dir=models.attempt_dir_name(2),
                ),
            ),
            artifacts=(
                models.EvidenceRef(
                    kind="artifact", locator=models.AUDIT_REPORT_FILENAME, digest=DIGEST
                ),
            ),
            issue_ids=("ASI-7", "ASI-9"),
            new_issue_count=1,
            recurring_issue_count=1,
            started_at=TIMESTAMP,
            completed_at=TIMESTAMP,
            duration_seconds=90.0,
        )

        self._assert_round_trips(summary, models.parse_assurance_summary)

    def test_observation_round_trips(self) -> None:
        observation = models.IssueObservation(
            run_id=RUN_ID,
            finding=_finding(),
            observed_at=TIMESTAMP,
            audit_artifact=models.AUDIT_REPORT_FILENAME,
        )

        self._assert_round_trips(observation, models.parse_issue_observation)

    def test_disposition_round_trips(self) -> None:
        disposition = models.Disposition(
            issue_id=42, kind="accept-risk", recorded_at=TIMESTAMP, note="known"
        )

        self.assertEqual("ASI-42", disposition.issue_id)
        self.assertEqual("accepted-risk", disposition.result_state)
        self._assert_round_trips(disposition, models.parse_disposition)

    def test_merge_round_trips(self) -> None:
        merge = models.IssueMerge(
            source_issue_id="ASI-9", target_issue_id=7, recorded_at=TIMESTAMP
        )

        self._assert_round_trips(merge, models.parse_issue_merge)

    def test_closeout_round_trips(self) -> None:
        closeout = models.Closeout(
            run_id=RUN_ID,
            outcome="reworked",
            recorded_at=TIMESTAMP,
            issue_ids=("ASI-7", 9),
            rework_minutes=25.0,
            note="fixed the retry clamp",
            plan_id="b766d9cd537c-c70d18f6ec8e",
            replaces="20260809T090000000000Z-11",
        )

        self._assert_round_trips(closeout, models.parse_closeout)

    def test_improvement_plan_draft_round_trips(self) -> None:
        draft = models.ImprovementPlanDraft(
            issue_id=7,
            harness="claude",
            output_path="/Users/operator/plans/asi-7.md",
            output_sha256=DIGEST,
            drafted_at=TIMESTAMP,
            harness_model="claude-opus-5",
        )

        self.assertEqual("ASI-7", draft.issue_id)
        self._assert_round_trips(draft, models.parse_improvement_plan_draft)

    def test_equal_durations_serialize_to_equal_bytes(self) -> None:
        integral = models.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=600,
        )
        fractional = models.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=600.0,
        )

        self.assertEqual(
            models.canonical_json_bytes(integral.to_dict()),
            models.canonical_json_bytes(fractional.to_dict()),
        )

    def test_canonical_bytes_are_key_order_independent(self) -> None:
        payload = _audit().to_dict()
        shuffled = dict(reversed(list(payload.items())))

        self.assertEqual(
            models.canonical_json_bytes(payload),
            models.canonical_json_bytes(shuffled),
        )


class ImprovementPlanDraftTests(unittest.TestCase):
    def _draft(self, **overrides) -> models.ImprovementPlanDraft:
        fields = {
            "issue_id": "ASI-7",
            "harness": "claude",
            "output_path": "/Users/operator/plans/asi-7.md",
            "output_sha256": DIGEST,
            "drafted_at": TIMESTAMP,
        }
        fields.update(overrides)
        return models.ImprovementPlanDraft(**fields)

    def test_the_model_carries_no_prompt_or_response_text(self) -> None:
        self.assertEqual(
            {
                "issue_id",
                "harness",
                "output_path",
                "output_sha256",
                "drafted_at",
                "harness_model",
            },
            set(self._draft().to_dict()),
        )

    def test_a_malformed_output_digest_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "SHA-256 hex digest"):
            self._draft(output_sha256="deadbeef")

    def test_an_unknown_harness_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'harness' must be one of"):
            self._draft(harness="oracle")

    def test_an_oversized_output_path_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "at most"):
            self._draft(output_path="x" * (models.MAX_LOCATOR_CHARS + 1))

    def test_unknown_keys_are_rejected(self) -> None:
        payload = self._draft().to_dict()
        payload["response"] = "the full model reply"

        with self.assertRaisesRegex(AssuranceError, "unknown key"):
            models.parse_improvement_plan_draft(payload)


class IssueIdTests(unittest.TestCase):
    def test_canonical_and_shorthand_forms_agree(self) -> None:
        self.assertEqual("ASI-42", models.parse_issue_id("ASI-42"))
        self.assertEqual("ASI-42", models.parse_issue_id("42"))
        self.assertEqual("ASI-42", models.parse_issue_id(42))
        self.assertEqual(42, models.issue_id_number("ASI-42"))

    def test_ambiguous_and_invalid_forms_are_rejected(self) -> None:
        for value in ("ASI-0", "0", "-1", "ASI-007", "asi-42", "ASI-", "", 0, True):
            with (
                self.subTest(value=value),
                self.assertRaises(AssuranceError),
            ):
                models.parse_issue_id(value)


class IdentityKeyTests(unittest.TestCase):
    def test_a_key_is_minted_from_an_allocated_issue_id(self) -> None:
        self.assertEqual("issue.asi-42", models.identity_key_for_issue("ASI-42"))
        self.assertEqual("issue.asi-42", models.identity_key_for_issue(42))
        self.assertEqual("ASI-42", models.issue_id_for_identity_key("issue.asi-42"))

    def test_keys_outside_the_minted_space_are_rejected(self) -> None:
        for value in (
            "issue.asi-0",
            "issue.asi-007",
            "issue.ASI-42",
            "ASI-42",
            "issue.asi-",
            "issue.asi-42 ",
            "asi-42",
            "",
            42,
            True,
            None,
        ):
            with self.subTest(value=value), self.assertRaises(AssuranceError):
                models.parse_identity_key(value)

    def test_an_oversized_key_is_rejected_before_it_is_matched(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "identity key"):
            models.parse_identity_key(f"issue.asi-{'1' * models.MAX_TOKEN_CHARS}")


class IssueIdentityBindingTests(unittest.TestCase):
    def _binding(self, **overrides) -> models.IssueIdentityBinding:
        payload = {
            "identity_key": "issue.asi-1",
            "issue_id": "ASI-1",
            "source": "allocation",
            "bound_at": TIMESTAMP,
        }
        payload.update(overrides)
        return models.IssueIdentityBinding(**payload)

    def test_a_binding_round_trips_through_its_parser(self) -> None:
        binding = self._binding(source="merge", issue_id="ASI-9", note="reviewed")
        self.assertEqual(
            binding, models.parse_issue_identity_binding(binding.to_dict())
        )

    def test_the_key_names_the_issue_it_was_minted_with(self) -> None:
        binding = self._binding(
            identity_key="issue.asi-2", issue_id="ASI-1", source="merge"
        )
        self.assertEqual("ASI-2", binding.named_issue_id)
        self.assertEqual("ASI-1", binding.issue_id)

    def test_an_allocation_binding_must_name_its_own_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "claims to be the key allocated"):
            self._binding(identity_key="issue.asi-2", issue_id="ASI-1")

    def test_a_merge_binding_may_point_a_key_at_another_issue(self) -> None:
        binding = self._binding(
            identity_key="issue.asi-2", issue_id="ASI-1", source="merge"
        )
        self.assertEqual("issue.asi-2", binding.identity_key)

    def test_an_unknown_source_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(source="renamed")

    def test_a_malformed_timestamp_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(bound_at="2026-08-10")

    def test_an_unknown_payload_key_is_rejected(self) -> None:
        payload = self._binding().to_dict()
        payload["reason"] = "the agent decided so"

        with self.assertRaisesRegex(AssuranceError, "unknown key"):
            models.parse_issue_identity_binding(payload)

    def test_an_oversized_note_is_rejected(self) -> None:
        with self.assertRaises(AssuranceError):
            self._binding(note="n" * (models.MAX_TEXT_CHARS + 1))


class NormalizedFindingTextTests(unittest.TestCase):
    def test_a_localization_pointer_normalizes_to_the_file_it_names(self) -> None:
        self.assertEqual(
            "agent_session_loop/runner.py",
            models.normalize_localization("agent_session_loop/runner.py:8129"),
        )
        self.assertEqual(
            "agent_session_loop/runner.py",
            models.normalize_localization("agent_session_loop/runner.py:8129:14"),
        )
        self.assertEqual(
            "agent_session_loop/runner.py",
            models.normalize_localization("agent_session_loop/runner.py"),
        )

    def test_two_sightings_of_one_file_normalize_together(self) -> None:
        self.assertEqual(
            models.normalize_localization("skills/asl/SKILL.md:12"),
            models.normalize_localization("skills/asl/SKILL.md:4001"),
        )

    def test_a_version_in_a_filename_survives_normalization(self) -> None:
        self.assertEqual(
            "agent_session_loop/state_v2.py",
            models.normalize_localization("agent_session_loop/state_v2.py"),
        )

    def test_summary_tokens_are_lowercased_sorted_and_deduplicated(self) -> None:
        self.assertEqual(
            ("after", "budget", "exhausted", "ran", "retry"),
            models.normalize_summary_tokens(
                "A retry ran after the budget was exhausted; the retry ran."
            ),
        )

    def test_rewording_a_summary_keeps_the_shared_tokens(self) -> None:
        first = set(
            models.normalize_summary_tokens(
                "A retry ran after the budget was exhausted."
            )
        )
        second = set(
            models.normalize_summary_tokens(
                "The retry budget was exhausted and a retry still ran."
            )
        )
        self.assertEqual({"budget", "exhausted", "retry", "ran"}, first & second)

    def test_a_negation_is_never_dropped_as_a_stopword(self) -> None:
        for word in ("not", "no", "never", "none", "nor", "without", "nothing"):
            with self.subTest(word=word):
                self.assertNotIn(word, models.SUMMARY_STOPWORDS)
        self.assertNotEqual(
            models.normalize_summary_tokens("The guard fired."),
            models.normalize_summary_tokens("The guard did not fire."),
        )

    def test_punctuation_and_path_separators_split_summary_tokens(self) -> None:
        self.assertEqual(
            ("guard", "py", "runner", "scope"),
            models.normalize_summary_tokens("`runner.py` scope-guard"),
        )

    def test_an_empty_summary_yields_no_tokens(self) -> None:
        self.assertEqual((), models.normalize_summary_tokens("   -- ,, "))

    def test_an_invariant_key_splits_into_sorted_segments(self) -> None:
        self.assertEqual(
            ("budget", "respected", "retry"),
            models.normalize_invariant_tokens("retry.budget-respected"),
        )

    def test_renaming_one_segment_keeps_the_others(self) -> None:
        self.assertEqual(
            {"budget", "retry"},
            set(models.normalize_invariant_tokens("retry.budget-respected"))
            & set(models.normalize_invariant_tokens("retry.budget-clamped")),
        )

    def test_unrelated_invariants_share_no_segment(self) -> None:
        self.assertEqual(
            set(),
            set(models.normalize_invariant_tokens("retry.budget-respected"))
            & set(models.normalize_invariant_tokens("scope.no-escape")),
        )

    def test_no_stopword_is_dropped_from_an_invariant_key(self) -> None:
        self.assertEqual(
            ("do", "not", "the", "thing"),
            models.normalize_invariant_tokens("the.do-not-thing"),
        )


class SummaryInvariantTests(unittest.TestCase):
    def _summary(self, **overrides) -> models.AssuranceSummary:
        payload = {"run_id": RUN_ID, "state": "pending", "eligible": True}
        payload.update(overrides)
        return models.AssuranceSummary(**payload)

    def test_counts_must_account_for_every_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must account for exactly"):
            self._summary(
                state="findings",
                issue_ids=("ASI-1", "ASI-2"),
                new_issue_count=2,
                recurring_issue_count=1,
            )

    def test_an_ineligible_run_can_only_be_skipped(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "ineligible run"):
            self._summary(state="pending", eligible=False)

    def test_skipping_creates_no_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "creates no issue"):
            self._summary(
                state="skipped",
                reason="operator-opted-out",
                issue_ids=("ASI-1",),
                new_issue_count=1,
            )

    def test_all_clear_creates_no_issue(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "creates no issues"):
            self._summary(state="all-clear", issue_ids=("ASI-1",), new_issue_count=1)

    def test_pending_has_not_run_yet(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "has not run"):
            self._summary(
                state="pending",
                attempts=(models.AuditAttempt(number=1, outcome="ok"),),
            )

    def test_findings_must_name_its_issues(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "must name the issue"):
            self._summary(state="findings")

    def test_attempts_are_numbered_consecutively_from_one(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "numbered consecutively"):
            self._summary(
                state="unavailable",
                reason="agent-timeout",
                attempts=(models.AuditAttempt(number=2, outcome="timeout"),),
            )

    def test_more_than_two_attempts_are_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "between 1 and 2"):
            models.AuditAttempt(number=3, outcome="ok")


class AuditRoleTests(unittest.TestCase):
    def test_the_audit_harness_must_differ_from_the_implementation_harness(
        self,
    ) -> None:
        with self.assertRaisesRegex(AssuranceError, "must differ"):
            models.AuditRole(
                harness="codex",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=600.0,
            )

    def test_the_audit_harness_may_equal_the_review_harness(self) -> None:
        role = models.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="review",
            timeout_seconds=600.0,
        )

        self.assertEqual("review", role.source)
        self.assertIsNone(role.model)

    def test_an_unknown_harness_is_rejected(self) -> None:
        with self.assertRaisesRegex(AssuranceError, "'harness' must be one of"):
            models.AuditRole(
                harness="gemini",
                implementation_harness="codex",
                source="explicit",
                timeout_seconds=600.0,
            )


class ArtifactWriterTests(unittest.TestCase):
    def test_a_canonical_artifact_is_written_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            written = models.write_audit_artifact(
                run_dir, models.AUDIT_REPORT_FILENAME, b"{}\n", durable=False
            )

            self.assertEqual(b"{}\n", written.read_bytes())
            with self.assertRaisesRegex(AssuranceError, "already exists"):
                models.write_audit_artifact(
                    run_dir, models.AUDIT_REPORT_FILENAME, b"{}\n", durable=False
                )
            self.assertEqual(b"{}\n", written.read_bytes())

    def test_traversal_artifact_names_are_rejected_not_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            for name in (
                "../escape.json",
                "nested/report.json",
                "/abs.json",
                ".hidden",
            ):
                with (
                    self.subTest(name=name),
                    self.assertRaises(AssuranceError),
                ):
                    models.write_audit_artifact(
                        Path(raw_dir), name, b"{}\n", durable=False
                    )

    def test_an_unbounded_payload_never_reaches_the_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            oversized = b"x" * (models.MAX_ARTIFACT_BYTES + 1)

            with self.assertRaisesRegex(AssuranceError, "over the"):
                models.write_audit_artifact(
                    run_dir, models.AUDIT_INPUT_FILENAME, oversized, durable=False
                )

            self.assertEqual([], list(run_dir.iterdir()))

    def test_audit_json_is_written_canonically(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            audit = _audit()
            path = models.write_audit_json(
                Path(raw_dir), models.AUDIT_REPORT_FILENAME, audit.to_dict()
            )

            self.assertEqual(
                models.canonical_json_bytes(audit.to_dict()), path.read_bytes()
            )
            self.assertEqual(
                audit,
                models.parse_supervisor_audit(
                    json.loads(path.read_text(encoding="utf-8"))
                ),
            )

    def test_each_attempt_gets_its_own_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            run_dir = Path(raw_dir)
            first = models.create_attempt_dir(run_dir, 1)
            second = models.create_attempt_dir(run_dir, 2)

            self.assertEqual("supervisor-audit-attempt-1", first.name)
            self.assertEqual("supervisor-audit-attempt-2", second.name)
            with self.assertRaisesRegex(AssuranceError, "already exists"):
                models.create_attempt_dir(run_dir, 1)


if __name__ == "__main__":
    unittest.main()
