# mypy: disable-error-code="attr-defined,name-defined"
# ruff: noqa: F403, F405
"""ASL assurance serialization and improvement-plan tests."""

# ruff: noqa: F403, F405
from tests.asl_assurance_models_support import *


class RoundTripTests(unittest.TestCase):
    def _assert_round_trips(self, model, parse) -> None:
        payload = model.to_dict()
        reparsed = parse(payload)

        self.assertEqual(model, reparsed)
        self.assertEqual(
            assurance_models_primitives.canonical_json_bytes(payload),
            assurance_models_primitives.canonical_json_bytes(reparsed.to_dict()),
        )

    def test_audit_round_trips(self) -> None:
        audit = _audit(
            state="findings",
            findings=(_finding(),),
            deterministic=(
                assurance_models_findings.DeterministicResult(
                    check="state-event-agreement",
                    lane="truthfulness-observability",
                    status="fail",
                    invariant_key="state.matches-events",
                    evidence=("state.json", "events.jsonl"),
                    detail="state says complete; events say paused",
                ),
            ),
        )

        self._assert_round_trips(audit, assurance_models_audit.parse_supervisor_audit)

    def test_summary_round_trips(self) -> None:
        summary = assurance_models_summary.AssuranceSummary(
            run_id=RUN_ID,
            state="findings",
            eligible=True,
            role=assurance_models_summary.AuditRole(
                harness="claude",
                implementation_harness="codex",
                source="review",
                timeout_seconds=600.0,
            ),
            attempts=(
                assurance_models_summary.AuditAttempt(
                    number=1, outcome="malformed-output"
                ),
                assurance_models_summary.AuditAttempt(
                    number=2,
                    outcome="ok",
                    duration_seconds=42.5,
                    artifact_dir=assurance_models_artifacts.attempt_dir_name(2),
                ),
            ),
            artifacts=(
                assurance_models_findings.EvidenceRef(
                    kind="artifact",
                    locator=assurance_models_vocab.AUDIT_REPORT_FILENAME,
                    digest=DIGEST,
                ),
            ),
            issue_ids=("ASI-7", "ASI-9"),
            new_issue_count=1,
            recurring_issue_count=1,
            started_at=TIMESTAMP,
            completed_at=TIMESTAMP,
            duration_seconds=90.0,
        )

        self._assert_round_trips(
            summary, assurance_models_summary.parse_assurance_summary
        )

    def test_observation_round_trips(self) -> None:
        observation = assurance_models_inbox.IssueObservation(
            run_id=RUN_ID,
            finding=_finding(),
            observed_at=TIMESTAMP,
            audit_artifact=assurance_models_vocab.AUDIT_REPORT_FILENAME,
        )

        self._assert_round_trips(
            observation, assurance_models_inbox.parse_issue_observation
        )

    def test_disposition_round_trips(self) -> None:
        disposition = assurance_models_inbox.Disposition(
            issue_id=42, kind="accept-risk", recorded_at=TIMESTAMP, note="known"
        )

        self.assertEqual("ASI-42", disposition.issue_id)
        self.assertEqual("accepted-risk", disposition.result_state)
        self._assert_round_trips(disposition, assurance_models_inbox.parse_disposition)

    def test_merge_round_trips(self) -> None:
        merge = assurance_models_inbox.IssueMerge(
            source_issue_id="ASI-9", target_issue_id=7, recorded_at=TIMESTAMP
        )

        self._assert_round_trips(merge, assurance_models_inbox.parse_issue_merge)

    def test_closeout_round_trips(self) -> None:
        closeout = assurance_models_inbox.Closeout(
            run_id=RUN_ID,
            outcome="reworked",
            recorded_at=TIMESTAMP,
            issue_ids=("ASI-7", 9),
            rework_minutes=25.0,
            note="fixed the retry clamp",
            plan_id="b766d9cd537c-c70d18f6ec8e",
            replaces="20260809T090000000000Z-11",
        )

        self._assert_round_trips(closeout, assurance_models_inbox.parse_closeout)

    def test_improvement_plan_draft_round_trips(self) -> None:
        draft = assurance_models_inbox.ImprovementPlanDraft(
            issue_id=7,
            harness="claude",
            output_path="/Users/operator/plans/asi-7.md",
            output_sha256=DIGEST,
            drafted_at=TIMESTAMP,
            harness_model="claude-opus-5",
        )

        self.assertEqual("ASI-7", draft.issue_id)
        self._assert_round_trips(
            draft, assurance_models_inbox.parse_improvement_plan_draft
        )

    def test_equal_durations_serialize_to_equal_bytes(self) -> None:
        integral = assurance_models_summary.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=600,
        )
        fractional = assurance_models_summary.AuditRole(
            harness="claude",
            implementation_harness="codex",
            source="explicit",
            timeout_seconds=600.0,
        )

        self.assertEqual(
            assurance_models_primitives.canonical_json_bytes(integral.to_dict()),
            assurance_models_primitives.canonical_json_bytes(fractional.to_dict()),
        )

    def test_canonical_bytes_are_key_order_independent(self) -> None:
        payload = _audit().to_dict()
        shuffled = dict(reversed(list(payload.items())))

        self.assertEqual(
            assurance_models_primitives.canonical_json_bytes(payload),
            assurance_models_primitives.canonical_json_bytes(shuffled),
        )


class ImprovementPlanDraftTests(unittest.TestCase):
    def _draft(self, **overrides) -> assurance_models_inbox.ImprovementPlanDraft:
        fields = {
            "issue_id": "ASI-7",
            "harness": "claude",
            "output_path": "/Users/operator/plans/asi-7.md",
            "output_sha256": DIGEST,
            "drafted_at": TIMESTAMP,
        }
        fields.update(overrides)
        return assurance_models_inbox.ImprovementPlanDraft(**fields)

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
            self._draft(
                output_path="x" * (assurance_models_vocab.MAX_LOCATOR_CHARS + 1)
            )

    def test_unknown_keys_are_rejected(self) -> None:
        payload = self._draft().to_dict()
        payload["response"] = "the full model reply"

        with self.assertRaisesRegex(AssuranceError, "unknown key"):
            assurance_models_inbox.parse_improvement_plan_draft(payload)
