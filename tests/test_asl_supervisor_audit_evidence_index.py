"""Evidence bundle references, scopes, redaction, and artifact indexing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agent_session_loop import (
    artifacts,
    assurance_models,
    events,
    mailbox,
    plans,
    repository_evidence,
    review,
    run_prompts,
    state,
    supervisor_audit_evidence,
)
from agent_session_loop.assurance_models import (
    AssuranceError,
)
from tests import support_supervisor_audit_evidence as evidence_support


class EvidenceBundleTests(evidence_support.EvidenceBundleFixture):
    """The audit input bundle is bounded, deterministic, and control-plane only."""

    def _rewrite_result(self, run_dir: Path, **fields: Any) -> None:
        path = run_dir / "turn-0001-01" / state.RESULT_FILE_NAME
        result = json.loads(path.read_text(encoding="utf-8"))
        result.update(fields)
        path.write_text(json.dumps(result), encoding="utf-8")

    def _rewrite_state(self, state_dir: Path, **fields: Any) -> None:
        path = state_dir / events.STATE_FILE_NAME
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(fields)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_the_mailbox_is_named_in_the_evidence_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))

            self.assertIn(
                f"state/{mailbox.MAILBOX_FILE_NAME}",
                {reference.locator for reference in bundle.evidence},
            )

    def test_every_reference_resolves_inside_an_approved_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = self._collect(*self._history(Path(tmp)))
            roots = set(bundle.payload["roots"])

            self.assertTrue(bundle.evidence)
            for reference in bundle.evidence:
                self.assertIn(reference.locator.split("/", 1)[0], roots)

    def test_paths_outside_the_approved_roots_are_withheld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            result_path = run_dir / "turn-0001-01" / state.RESULT_FILE_NAME
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["invocation"]["cwd"] = "/etc/ssh/secret_config"
            result_path.write_text(json.dumps(result), encoding="utf-8")

            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)
            text = bundle.to_bytes().decode("utf-8")

            self.assertNotIn("/etc/ssh/secret_config", text)
            self.assertIn(supervisor_audit_evidence.REASON_OUTSIDE_APPROVED_ROOTS, text)

    def test_repository_fingerprints_are_reported_as_not_recorded_when_absent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload

            self.assertFalse(payload["repository"]["available"])
            self.assertEqual(
                supervisor_audit_evidence.REASON_NOT_RECORDED,
                payload["repository"]["reason"],
            )

    def test_repository_fingerprints_are_copied_when_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            snapshot = repository_evidence.capture_workspace_snapshot(
                workspace, plan_dir
            )

            payload = self._collect(
                workspace,
                plan_dir,
                state_dir,
                run_dir,
                workspace_snapshot=snapshot,
            ).payload

            self.assertTrue(payload["repository"]["available"])
            self.assertIn("app", payload["repository"]["repositories"])

    def test_the_bundle_writes_the_canonical_audit_input_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)

            written = bundle.write(run_dir, durable=False)

            self.assertEqual(bundle.to_bytes(), written.read_bytes())
            with self.assertRaises(AssuranceError):
                bundle.write(run_dir, durable=False)

    def test_scopes_and_versions_describe_the_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload

            self.assertEqual(
                [
                    {
                        "session": "session-01",
                        "available": True,
                        "reads": ["app"],
                        "writes": ["app"],
                    }
                ],
                payload["scopes"]["sessions"],
            )
            self.assertTrue(payload["versions"]["package"]["available"])
            self.assertEqual(
                events.EVENT_SCHEMA_VERSION, payload["versions"]["event_schema_version"]
            )

    def test_scope_projection_uses_only_authoritative_session_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            for name in (
                "session-01.expand.md",
                "session-01.replay-proof.md",
                "session-01.notes.md",
                "session-01.contract.md",
                "session-01-dev-owl-update-check.md",
                "session-01-live-ticket-recheck.md",
                "session-01-supervisor-diagnosis.md",
            ):
                (history[1] / name).write_text(
                    "Supporting evidence, not a session.\n", encoding="utf-8"
                )
            section = self._collect(*history).payload["scopes"]
            self.assertTrue(section["available"])
            self.assertEqual(
                ["session-01"], [row["session"] for row in section["sessions"]]
            )
            self.assertTrue(section["sessions"][0]["available"])

    def test_an_actual_session_with_malformed_scope_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            history = self._history(Path(tmp))
            (history[1] / "session-02.md").write_text(
                "No scope declared.\n", encoding="utf-8"
            )
            section = self._collect(*history).payload["scopes"]
            row = next(
                row for row in section["sessions"] if row["session"] == "session-02"
            )
            self.assertFalse(row["available"])
            self.assertEqual(supervisor_audit_evidence.REASON_MALFORMED, row["reason"])

    def test_generated_bytecode_never_moves_a_tree_digest(self) -> None:
        """Running an interpreter over a skill tree must not restate its version."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "SKILL.md"
            source.write_text("version one\n", encoding="utf-8")
            bytecode = root / "scripts" / "__pycache__" / "tool.cpython-314.pyc"
            bytecode.parent.mkdir(parents=True)
            bytecode.write_bytes(b"one")

            before = supervisor_audit_evidence._tree_digest(root, patterns=("*",))
            bytecode.write_bytes(b"two-and-rather-longer")
            after = supervisor_audit_evidence._tree_digest(root, patterns=("*",))
            source.write_text("version two\n", encoding="utf-8")
            edited = supervisor_audit_evidence._tree_digest(root, patterns=("*",))

            self.assertEqual(before, after)
            self.assertEqual(1, before["file_count"])
            self.assertNotEqual(after["sha256"], edited["sha256"])

    def test_authorized_aliases_survive_redaction_but_secret_keys_do_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                workspace_write_guard={
                    "passed": True,
                    "violations": [],
                    "unavailable": {},
                    "authorized_aliases": ["app"],
                },
                auth_token=self.SECRET_ARGV,
                authorization=self.SECRET_ARGV,
            )

            result = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "turns"
            ][0]["result"]

            self.assertEqual(
                ["app"], result["workspace_write_guard"]["authorized_aliases"]
            )
            self.assertEqual(supervisor_audit_evidence.REDACTED, result["auth_token"])
            self.assertEqual(
                supervisor_audit_evidence.REDACTED, result["authorization"]
            )

    def test_a_reply_origin_survives_while_a_remote_is_canonicalized(self) -> None:
        for origin in run_prompts.REPLY_ORIGINS:
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_result(
                    run_dir, reply_source={"origin": origin, "reply_length": 6}
                )

                payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

                self.assertEqual(
                    origin, payload["turns"][0]["result"]["reply_source"]["origin"]
                )
                self.assertEqual(
                    "github.com/owner/repo",
                    payload["registry"]["repositories"][0]["origin"],
                )

    def test_valid_audit_role_sources_survive_but_arbitrary_source_is_redacted(
        self,
    ) -> None:
        for source in assurance_models.AUDIT_ROLE_SOURCES:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_state(
                    state_dir,
                    assurance={
                        "role": {"source": source},
                        "source": "arbitrary application text",
                    },
                )

                status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                    "status"
                ]

                self.assertEqual(source, status["assurance"]["role"]["source"])
                self.assertIn(
                    supervisor_audit_evidence.REASON_APPLICATION_CONTENT,
                    status["assurance"]["source"],
                )

    def test_control_plane_signal_sources_survive_under_any_parent(self) -> None:
        # The runner stamps this provenance under several parents, so the
        # exemption is keyed on the value rather than on the parent name.
        for source in sorted(supervisor_audit_evidence._CONTROL_PLANE_SOURCE_VALUES):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
                self._rewrite_state(
                    state_dir,
                    action_required={"reason": "runner-pause", "source": source},
                    pause_details={"source": source},
                )

                status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                    "status"
                ]

                self.assertEqual(source, status["action_required"]["source"])
                self.assertEqual(source, status["pause_details"]["source"])

    def test_an_unbounded_source_is_still_withheld_as_application_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                action_required={"source": "arbitrary application text"},
                pause_details={"source": "stdout of the agent, quoted at length"},
            )

            status = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "status"
            ]

            self.assertIn(
                supervisor_audit_evidence.REASON_APPLICATION_CONTENT,
                status["action_required"]["source"],
            )
            self.assertIn(
                supervisor_audit_evidence.REASON_APPLICATION_CONTENT,
                status["pause_details"]["source"],
            )

    def test_final_review_artifacts_are_indexed_from_the_plan_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            review.final_review_output_path(plan_dir).write_text(
                f"{review.REVIEW_STATUS_ALL_CLEAR}\n", encoding="utf-8"
            )
            review.final_review_artifact_path(plan_dir).write_text(
                json.dumps(
                    {
                        "version": review.REVIEW_ARTIFACT_VERSION,
                        "outcome": review.REVIEW_OUTCOME_ALL_CLEAR,
                        "findings": [],
                    }
                ),
                encoding="utf-8",
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            digests = {
                entry["locator"]: entry["digest"] for entry in payload["evidence"]
            }
            self.assertIn(f"plan/{review.FINAL_REVIEW_FILENAME}", digests)
            self.assertIn(f"plan/{review.FINAL_REVIEW_ARTIFACT_FILENAME}", digests)
            # A locator alone cannot tell the auditor whether the indexed file
            # is the one this run produced, so every row carries the digest of
            # the bytes on disk.
            for path in (
                review.final_review_output_path(plan_dir),
                review.final_review_artifact_path(plan_dir),
            ):
                self.assertEqual(
                    artifacts.sha256_hex(path.read_bytes()),
                    digests[f"plan/{path.name}"],
                )
            self.assertEqual(
                [],
                [
                    entry
                    for entry in payload["omissions"]
                    if entry["section"] == "review.artifacts"
                ],
            )

    def test_canonical_audit_artifacts_are_indexed_with_their_digests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            written = {}
            for name in assurance_models.CANONICAL_AUDIT_ARTIFACTS:
                body = f"{name} body".encode()
                (run_dir / name).write_bytes(body)
                written[name] = artifacts.sha256_hex(body)

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            digests = {
                entry["locator"]: entry["digest"] for entry in payload["evidence"]
            }
            for name, digest in written.items():
                self.assertEqual(digest, digests[f"run/{name}"])

    def test_absent_final_review_artifacts_are_explained_by_an_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self.assertFalse(review.final_review_output_path(plan_dir).exists())

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            locators = {entry["locator"] for entry in payload["evidence"]}
            self.assertNotIn(f"plan/{review.FINAL_REVIEW_FILENAME}", locators)
            self.assertEqual(
                [
                    {
                        "section": "review.artifacts",
                        "reason": supervisor_audit_evidence.REASON_MISSING,
                        "count": 2,
                        "sha256": mock.ANY,
                    }
                ],
                [
                    entry
                    for entry in payload["omissions"]
                    if entry["section"] == "review.artifacts"
                ],
            )

    def test_a_failed_write_guard_makes_its_turn_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            # The turn exits zero and reports a complete status: the guard
            # result is the only record that it wrote outside its scope.
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": False,
                    "violations": [{"kind": "repository-added", "alias": "app"}],
                    "unavailable": {},
                    "reason": events.PAUSE_REASON_WORKSPACE_WRITE_SCOPE_VIOLATION,
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertTrue(payload["turns"][0]["anomalous"])

    def test_a_passing_write_guard_leaves_its_turn_unremarkable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": True,
                    "violations": [],
                    "unavailable": {},
                    "authorized_aliases": ["app"],
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertFalse(payload["turns"][0]["anomalous"])

    def test_an_unjudgeable_write_guard_makes_its_turn_anomalous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_result(
                run_dir,
                status_result="complete",
                workspace_write_guard={
                    "passed": False,
                    "violations": [],
                    "unavailable": {"app": {"reason": "unreadable"}},
                },
            )

            payload = self._collect(workspace, plan_dir, state_dir, run_dir).payload

            self.assertTrue(payload["turns"][0]["anomalous"])

    def test_the_progress_journal_is_indexed_with_its_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            journal = plans.progress_path(plan_dir)
            journal.write_text("# Progress Journal\n\n## Session 1\n", encoding="utf-8")

            bundle = self._collect(workspace, plan_dir, state_dir, run_dir)
            entry = bundle.payload["plan"]["progress"]
            indexed = {reference.locator: reference for reference in bundle.evidence}

            self.assertTrue(entry["available"])
            self.assertIn(entry["reference"], indexed)
            self.assertEqual(
                artifacts.sha256_hex(journal.read_bytes()),
                indexed[entry["reference"]].digest,
            )

    def test_a_missing_progress_journal_is_recorded_as_an_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._collect(*self._history(Path(tmp))).payload
            entry = payload["plan"]["progress"]

            self.assertFalse(entry["available"])
            self.assertEqual(supervisor_audit_evidence.REASON_MISSING, entry["reason"])
            self.assertTrue(
                any(
                    omission["section"] == "plan"
                    and omission["reason"] == supervisor_audit_evidence.REASON_MISSING
                    for omission in payload["omissions"]
                )
            )

    def test_the_observed_model_is_projected_beside_the_configured_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                model="native CLI default",
                model_source="unresolved",
                observed_model="claude-opus-5",
                observed_model_source="harness_reported",
            )

            section = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "state"
            ]

            self.assertEqual("native CLI default", section["model"])
            self.assertEqual("unresolved", section["model_source"])
            self.assertEqual("native CLI default", section["requested_model"])
            self.assertEqual("unresolved", section["requested_model_source"])
            self.assertEqual("claude-opus-5", section["observed_model"])
            self.assertEqual("harness_reported", section["observed_model_source"])

    def test_the_per_role_model_projection_reaches_the_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace, plan_dir, state_dir, run_dir = self._history(Path(tmp))
            self._rewrite_state(
                state_dir,
                model="native CLI default",
                model_source="unresolved",
                model_projection=[
                    {
                        "session_kind": "implementation",
                        "harness": "claude",
                        "requested_model": "native CLI default",
                        "requested_model_source": "unresolved",
                        "observed_models": ["claude-opus-5"],
                        "turns": 3,
                        "model": "claude-opus-5",
                        "model_source": "harness_reported",
                    }
                ],
            )

            section = self._collect(workspace, plan_dir, state_dir, run_dir).payload[
                "state"
            ]

            # An audit that saw only the unresolved request could not tell that
            # the run's own turns had already answered the question.
            self.assertEqual(
                [("implementation", "claude-opus-5", "harness_reported")],
                [
                    (record["session_kind"], record["model"], record["model_source"])
                    for record in section["model_projection"]
                ],
            )

    def test_a_run_without_a_projection_records_an_empty_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            section = self._collect(*self._history(Path(tmp))).payload["state"]

            self.assertEqual([], section["model_projection"])


if __name__ == "__main__":
    unittest.main()
