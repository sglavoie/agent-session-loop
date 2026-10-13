"""Prepare rebind staging and validator correction tests."""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    authoring_transaction,
    plans_errors,
    plans_files,
    plans_metadata,
    preflight_cli,
    prepare,
    prepare_expand_ingest,
    prepare_models,
    prepare_packet,
    prepare_results,
    state_locks,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _prepare_pipeline_payload,
    _recording_validator,
    init_multi_repo_workspace,
)
from tests.support_asl_prepare import PrepareEndToEndFixtures


class PrepareRebindAndCorrectionTests(PrepareEndToEndFixtures):
    def test_rebind_stage_failures_preserve_ready_plan_byte_for_byte(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None
        before = self._plan_file_bytes(first.plan_dir)

        invalid = self._prepare(
            None,
            _recording_validator([]),
            cwd=self.tmp_path / "missing-workspace",
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )
        self.assertFalse(invalid.ok)
        assert invalid.workspace_rebind is not None
        self.assertEqual(
            prepare_results.PREPARE_STAGE_WORKSPACE,
            invalid.workspace_rebind["failure_stage"],
        )
        self.assertEqual(before, self._plan_file_bytes(first.plan_dir))

        def run_rebind(validator=None) -> prepare_results.PrepareResult:
            return self._prepare(
                None,
                validator or _recording_validator([]),
                cwd=new_workspace,
                resume_plan_dir=first.plan_dir,
                rebind_workspace=True,
            )

        injections = []
        injections.append(
            (
                "metadata",
                mock.patch.object(
                    plans_metadata,
                    "save_workspace_plan_metadata",
                    side_effect=plans_errors.PlanError("injected metadata failure"),
                ),
                _recording_validator([]),
                prepare_results.PREPARE_STAGE_METADATA,
            )
        )
        injections.append(
            (
                "validation",
                nullcontext(),
                lambda _packet: (_ for _ in ()).throw(
                    plans_errors.PlanError("injected validation failure")
                ),
                prepare_results.PREPARE_STAGE_VALIDATION,
            )
        )
        injections.append(
            (
                "expansion",
                mock.patch.object(
                    prepare_expand_ingest,
                    "render_expansion",
                    side_effect=plans_errors.PlanError("injected expansion failure"),
                ),
                _recording_validator([]),
                prepare_results.PREPARE_STAGE_VALIDATION,
            )
        )
        injections.append(
            (
                "correction",
                mock.patch.object(
                    prepare_expand_ingest,
                    "_apply_one_correction",
                    side_effect=plans_errors.PlanError("injected correction failure"),
                ),
                _recording_validator(
                    [],
                    corrections_by_id={
                        "1": [
                            {
                                "expected_text": "Implement `SVC:demo/module.py`.",
                                "replacement_text": "Implement `SVC:demo/new.py`.",
                                "reason": "injected",
                            }
                        ]
                    },
                ),
                prepare_results.PREPARE_STAGE_VALIDATION,
            )
        )
        unsatisfied = {
            "check_count": 1,
            "reused": False,
            "checks": [
                {
                    "session_id": "1",
                    "name": "injected",
                    "expectation": "pass",
                    "status": "fail",
                    "satisfied": False,
                    "detail": "injected baseline failure",
                }
            ],
        }
        unsatisfied["unsatisfied"] = list(unsatisfied["checks"])
        injections.append(
            (
                "baseline",
                mock.patch.object(
                    prepare, "_run_baseline_stage", return_value=unsatisfied
                ),
                _recording_validator([]),
                prepare_results.PREPARE_STAGE_BASELINE,
            )
        )
        injections.append(
            (
                "preflight",
                mock.patch.object(
                    preflight_cli,
                    "_mark_loop_ready",
                    return_value=(False, "injected preflight failure"),
                ),
                _recording_validator([]),
                prepare_results.PREPARE_STAGE_PREFLIGHT,
            )
        )

        original_replace = os.replace

        def fail_publication(source, destination):
            target = Path(destination)
            if (
                target.parent == first.plan_dir
                and target.name == plans_metadata.PLAN_METADATA_FILE_NAME
            ):
                raise OSError("injected publication failure")
            return original_replace(source, destination)

        injections.append(
            (
                "publication",
                mock.patch.object(
                    authoring_transaction.os, "replace", side_effect=fail_publication
                ),
                _recording_validator([]),
                prepare_results.PREPARE_STAGE_PUBLICATION,
            )
        )

        for name, injection, validator, expected_stage in injections:
            with self.subTest(stage=name), injection:
                result = run_rebind(validator)
                self.assertFalse(result.ok)
                assert result.workspace_rebind is not None
                self.assertEqual(
                    expected_stage, result.workspace_rebind["failure_stage"]
                )
                self.assertEqual(before, self._plan_file_bytes(first.plan_dir))
                self.assertIsNone(
                    state_locks.inspect_lock_info(first.plan_dir, self.state_dir)
                )

    def test_rebind_rejects_unchanged_legacy_and_active_bindings(self) -> None:
        legacy_source = self._write_source()
        legacy = self._prepare(legacy_source, _recording_validator([]))
        assert legacy.plan_dir is not None
        with self.assertRaisesRegex(plans_errors.PlanError, "registered plan"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=self.repo,
                resume_plan_dir=legacy.plan_dir,
                rebind_workspace=True,
            )

        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        registered_source = self._write_registered_source(specs)
        registered = self._prepare(
            registered_source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        assert registered.plan_dir is not None
        with self.assertRaisesRegex(plans_errors.PlanError, "changed workspace"):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=old_workspace,
                resume_plan_dir=registered.plan_dir,
                rebind_workspace=True,
            )

        before = self._plan_file_bytes(registered.plan_dir)
        state_locks.acquire_lock(registered.plan_dir, self.state_dir, owner="test")
        try:
            with self.assertRaisesRegex(plans_errors.PlanError, "active lock"):
                self._prepare(
                    None,
                    _recording_validator([]),
                    cwd=new_workspace,
                    resume_plan_dir=registered.plan_dir,
                    rebind_workspace=True,
                )
        finally:
            state_locks.release_lock(registered.plan_dir, self.state_dir)
        self.assertEqual(before, self._plan_file_bytes(registered.plan_dir))

    def test_rebind_accepts_a_legacy_binding_once_a_registry_appears(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        init_multi_repo_workspace(new_workspace, specs)
        registered = self._prepare(
            self._write_registered_source(specs),
            _recording_validator([]),
            cwd=old_workspace,
        )
        assert registered.plan_dir is not None
        # Simulate the plan that grew a registry only after it was bound in
        # legacy mode: the binding on disk is still version 1.
        plans_metadata.save_plan_metadata(registered.plan_dir, self.repo)

        rebound = self._prepare(
            None,
            _recording_validator([]),
            cwd=new_workspace,
            resume_plan_dir=registered.plan_dir,
            rebind_workspace=True,
        )

        self.assertTrue(rebound.ok, rebound)
        assert rebound.workspace_rebind is not None
        self.assertEqual(
            str(plans_files.canonicalize_path(self.repo)),
            rebound.workspace_rebind["old"]["workspace_root"],
        )
        self.assertEqual([], rebound.workspace_rebind["old"]["repositories"])
        self.assertEqual(
            str(plans_files.canonicalize_path(new_workspace)),
            rebound.workspace_rebind["new"]["workspace_root"],
        )
        metadata = plans_metadata.load_plan_metadata(registered.plan_dir)
        self.assertIsInstance(metadata, plans_metadata.WorkspacePlanMetadata)
        assert isinstance(metadata, plans_metadata.WorkspacePlanMetadata)
        self.assertEqual(
            str(plans_files.canonicalize_path(new_workspace)), metadata.workspace_root
        )

    def test_repository_ambiguity_issue_prevents_loop_ready(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)

        def ambiguous_validator(packet: prepare_packet.ValidationTaskPacket) -> object:
            payload = _prepare_pipeline_payload(
                packet.codebase_root,
                packet.session_ids,
                version=packet.version,
                repositories=packet.repositories,
                session_repositories=packet.session_read_repositories,
            )
            payload["sessions"][0]["issues"] = [
                {
                    "issue_type": prepare_models.REPOSITORY_AMBIGUITY_ISSUE,
                    "summary": "`demo/module.py` is not repository-qualified",
                    "session_ref": "`demo/module.py`",
                    "actual": "the path exists in more than one read repository",
                    "fix": "qualify the path as SVC:demo/module.py",
                    "repo": "SVC",
                }
            ]
            return payload

        result = self._prepare(source, ambiguous_validator, cwd=workspace)

        self.assertFalse(result.ok)
        self.assertEqual(
            prepare_results.PREPARE_EXIT_REASON_VALIDATION_FAILED, result.reason
        )
        self.assertFalse(result.marked_loop_ready)
        self.assertEqual(
            prepare_models.REPOSITORY_AMBIGUITY_ISSUE, result.blockers[0].category
        )
        assert result.plan_dir is not None
        self.assertIn(
            "**Readiness**: draft",
            (result.plan_dir / "manifest.md").read_text(encoding="utf-8"),
        )

    def test_malformed_v2_output_does_not_bind_registered_plan(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)

        result = self._prepare(
            source,
            _recording_validator([], payload_override={"bogus": True}),
            cwd=workspace,
        )

        self.assertFalse(result.ok)
        assert result.plan_dir is not None
        self.assertFalse(
            (result.plan_dir / plans_metadata.PLAN_METADATA_FILE_NAME).exists()
        )
        self.assertFalse(
            (result.plan_dir / prepare_models.PREPARE_VALIDATION_FILENAME).exists()
        )
        self.assertEqual([], list(result.plan_dir.glob("*.expand.md")))

    def test_tolerant_headings_author_canonical_plan_artifacts(self) -> None:
        source = self._write_source(
            heading_by_id={
                "1": " ### SeSsIoN 01 - Prime Setup ###   ",
                "2": "###### SESSION 002 — Finalize ####",
            }
        )
        calls: list[prepare_packet.ValidationTaskPacket] = []

        result = self._prepare(source, _recording_validator(calls))

        self.assertTrue(result.ok, result)
        self.assertEqual(("1", "2"), result.session_ids)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        plan_dir = result.plan_dir
        assert plan_dir is not None
        self.assertTrue((plan_dir / "session-1.md").is_file())
        self.assertTrue((plan_dir / "session-2.md").is_file())
        self.assertFalse((plan_dir / "session-01.md").exists())
        self.assertFalse((plan_dir / "session-002.md").exists())

        session_one = (plan_dir / "session-1.md").read_text(encoding="utf-8")
        session_two = (plan_dir / "session-2.md").read_text(encoding="utf-8")
        self.assertTrue(session_one.startswith("# Session 1: Prime Setup\n"))
        self.assertTrue(session_two.startswith("# Session 2: Finalize\n"))
        self.assertIn('"session": "1"', session_one)
        self.assertIn('"session": "2"', session_two)

        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("| 1 | Prime Setup | pending | session-1.md |", manifest)
        self.assertIn("| 2 | Finalize | pending | session-2.md |", manifest)
        self.assertNotIn("| 01 |", manifest)
        self.assertNotIn("| 002 |", manifest)

    def test_validator_corrections_are_applied_to_session_files(self) -> None:
        source = self._write_source(
            body_by_id={"1": "Implement step 1 in `demo/old_module.py`."}
        )
        corrections = {
            "1": [
                {
                    "expected_text": "`demo/old_module.py`",
                    "replacement_text": "`demo/module.py`",
                    "reason": "module was renamed",
                }
            ]
        }

        result = self._prepare(
            source,
            _recording_validator([], corrections_by_id=corrections),
        )

        self.assertTrue(result.ok, result)
        self.assertEqual(1, len(result.corrections))
        self.assertEqual(
            prepare_models.CORRECTION_APPLIED, result.corrections[0].status
        )
        assert result.plan_dir is not None
        session_text = (result.plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertIn("`demo/module.py`", session_text)
        self.assertNotIn("`demo/old_module.py`", session_text)
