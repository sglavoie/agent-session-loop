"""End-to-end prepare against registered workspaces."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from unittest import mock

from agent_session_loop import (
    authoring,
    baseline,
    baseline_cache,
    contracts,
    plans,
    prepare_baseline,
    prepare_models,
    prepare_packet,
    prepare_validation_cache,
    prepare_validation_store,
    state,
    verification,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    _recording_validator,
    init_multi_repo_workspace,
)
from tests.support_asl_prepare import PrepareEndToEndFixtures


class PrepareRegisteredWorkspaceTests(PrepareEndToEndFixtures):
    def test_clean_source_plan_produces_loop_ready_directory(self) -> None:
        source = self._write_source()
        calls: list[prepare_packet.ValidationTaskPacket] = []

        result = self._prepare(source, _recording_validator(calls))

        self.assertTrue(result.ok, result)
        self.assertEqual("ok", result.reason)
        self.assertTrue(result.marked_loop_ready)
        plan_dir = result.plan_dir
        assert plan_dir is not None
        for name in (
            "context.md",
            "manifest.md",
            "progress.md",
            "session-1.md",
            "session-2.md",
            "session-1.expand.md",
            "session-2.expand.md",
            prepare_models.PREPARE_VALIDATION_FILENAME,
            prepare_models.PREPARE_VALIDATION_CACHE_FILENAME,
            plans.PLAN_METADATA_FILE_NAME,
        ):
            self.assertTrue((plan_dir / name).is_file(), name)
        self.assertEqual(
            str(plans.canonicalize_path(self.repo)),
            plans.load_plan_metadata(plan_dir).cwd,
        )
        manifest = (plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        session_text = (plan_dir / "session-1.md").read_text(encoding="utf-8")
        self.assertEqual(1, session_text.count(contracts.COMPLETION_CONTRACT_HEADING))
        self.assertIn("> **Expansion notes**", session_text)
        # One validation batch covered both sessions.
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        canonical_repo = plans.canonicalize_path(self.repo)
        self.assertEqual(
            shlex.join(
                [
                    "asl",
                    "run",
                    str(plan_dir),
                    "--cwd",
                    str(canonical_repo),
                    "--harness",
                    "codex",
                    "--harness-model",
                    "gpt-5",
                    "--review-harness",
                    "claude",
                    "--review-harness-model",
                    "claude-opus-4",
                ]
            ),
            result.run_command,
        )
        assert result.resume_command is not None
        self.assertIn("--validator-harness claude", result.resume_command)
        self.assertIn("--harness-model gpt-5", result.resume_command)
        self.assertIn("--review-harness claude", result.resume_command)
        self.assertIn("--review-harness-model claude-opus-4", result.resume_command)
        self.assertEqual(
            ["ok", "ok", "ok", "ok", "ok", "ok"],
            [step.status for step in result.steps],
        )

    def test_registered_prepare_uses_v3_and_repository_aware_baselines(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        repositories = init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        calls: list[prepare_packet.ValidationTaskPacket] = []

        result = self._prepare(
            source,
            _recording_validator(calls),
            cwd=workspace,
        )

        self.assertTrue(result.ok, result)
        self.assertEqual(1, len(calls))
        self.assertEqual(prepare_models.PREPARE_VALIDATION_VERSION, calls[0].version)
        assert result.plan_dir is not None
        metadata = plans.load_plan_metadata(result.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(3, metadata.version)
        validation = prepare_validation_store.load_prepare_validation(result.plan_dir)
        assert validation is not None
        self.assertEqual(prepare_models.PREPARE_VALIDATION_VERSION, validation.version)
        self.assertEqual(
            ("SVC", "OAK"), tuple(r.alias for r in validation.repositories)
        )
        assert result.baseline is not None
        self.assertEqual(2, result.baseline["check_count"])
        self.assertEqual(
            {str(path.resolve()) for path in repositories.values()},
            {entry["resolved_root"] for entry in result.baseline["checks"]},
        )

    def test_registered_cached_baselines_capture_fingerprint_once(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        workspace = self.tmp_path / "workspace"
        init_multi_repo_workspace(workspace, specs)
        source = self._write_registered_source(specs)
        result = self._prepare(
            source,
            _recording_validator([]),
            cwd=workspace,
        )
        self.assertTrue(result.ok, result)
        assert result.plan_dir is not None
        context = verification.verification_context_from_plan(
            result.plan_dir, workspace
        )
        collected = baseline.collect_baseline_checks(
            {
                session.id: contracts.read_completion_contract(
                    result.plan_dir, plans.parse_session_id(session.id)
                )
                for session in plans.inspect_plan(result.plan_dir).sessions
            }
        )
        unique_checks = [
            (
                baseline.baseline_identity_key(check, context.root, context=context),
                session_id,
                check,
            )
            for session_id, check in collected
        ]
        original = baseline.capture_baseline_repository_evidence

        with mock.patch.object(
            baseline,
            "capture_baseline_repository_evidence",
            wraps=original,
        ) as capture:
            entries = prepare_baseline._cached_baseline_entries(
                result.plan_dir, context, unique_checks
            )

        self.assertIsNotNone(entries)
        self.assertEqual(2, len(entries))
        capture.assert_called_once()

    def test_registered_resume_rebinds_all_workspace_evidence_atomically(self) -> None:
        specs = DEFAULT_MULTI_REPO_SPECS[:2]
        old_workspace = self.tmp_path / "old-workspace"
        new_workspace = self.tmp_path / "new-workspace"
        init_multi_repo_workspace(old_workspace, specs)
        new_repositories = init_multi_repo_workspace(new_workspace, specs)
        source = self._write_registered_source(specs)
        first = self._prepare(
            source,
            _recording_validator([]),
            cwd=old_workspace,
        )
        self.assertTrue(first.ok, first)
        assert first.plan_dir is not None

        calls: list[prepare_packet.ValidationTaskPacket] = []
        rebound = self._prepare(
            None,
            _recording_validator(calls),
            cwd=new_workspace,
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )

        self.assertTrue(rebound.ok, rebound)
        self.assertEqual(1, len(calls))
        self.assertEqual(("1", "2"), calls[0].session_ids)
        metadata = plans.load_plan_metadata(first.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(str(new_workspace.resolve()), metadata.workspace_root)
        aggregate = prepare_validation_store.load_prepare_validation(first.plan_dir)
        assert aggregate is not None
        self.assertEqual(str(new_workspace.resolve()), aggregate.workspace_root)
        self.assertEqual(
            {str(path.resolve()) for path in new_repositories.values()},
            {repository.root for repository in aggregate.repositories},
        )
        validator_cache = prepare_validation_cache.load_prepare_validation_cache(
            first.plan_dir
        )
        cache_entry = validator_cache.entry("1")
        assert cache_entry is not None
        self.assertEqual(
            str(new_workspace.resolve()), cache_entry.validation.workspace_root
        )
        expansion = (first.plan_dir / "session-1.expand.md").read_text(encoding="utf-8")
        self.assertIn(str(new_workspace.resolve()), expansion)
        self.assertNotIn(str(old_workspace.resolve()), expansion)
        cache_text = (
            first.plan_dir / baseline_cache.BASELINE_RESULTS_FILENAME
        ).read_text(encoding="utf-8")
        self.assertIn(str(new_workspace.resolve()), cache_text)
        self.assertNotIn(str(old_workspace.resolve()), cache_text)
        manifest = (first.plan_dir / "manifest.md").read_text(encoding="utf-8")
        self.assertIn("**Readiness**: loop-ready", manifest)
        assert rebound.workspace_rebind is not None
        self.assertEqual(
            str(old_workspace.resolve()),
            rebound.workspace_rebind["old"]["workspace_root"],
        )
        self.assertEqual(
            str(new_workspace.resolve()),
            rebound.workspace_rebind["new"]["workspace_root"],
        )
        self.assertIsNone(rebound.workspace_rebind["failure_stage"])
        self.assertNotIn("git@", json.dumps(rebound.workspace_rebind))
        self.assertIn("--cwd " + str(new_workspace.resolve()), rebound.run_command)
        self.assertIn("--rebind-workspace", rebound.resume_command)
        self.assertIsNone(state.inspect_lock_info(first.plan_dir, self.state_dir))

    def test_interrupted_rebind_publication_rolls_forward_on_next_read(self) -> None:
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

        original_replace = os.replace

        def interrupt_publication(source_path, destination):
            target = Path(destination)
            if (
                target.parent == first.plan_dir
                and target.name == plans.PLAN_METADATA_FILE_NAME
            ):
                raise KeyboardInterrupt("injected publication interruption")
            return original_replace(source_path, destination)

        with (
            mock.patch.object(
                authoring.os, "replace", side_effect=interrupt_publication
            ),
            self.assertRaisesRegex(KeyboardInterrupt, "publication interruption"),
        ):
            self._prepare(
                None,
                _recording_validator([]),
                cwd=new_workspace,
                resume_plan_dir=first.plan_dir,
                rebind_workspace=True,
            )

        self.assertTrue((first.plan_dir / plans.PLAN_TRANSACTION_DIR_NAME).is_dir())
        metadata = plans.load_plan_metadata(first.plan_dir)
        self.assertIsInstance(metadata, plans.WorkspacePlanMetadata)
        assert isinstance(metadata, plans.WorkspacePlanMetadata)
        self.assertEqual(str(new_workspace.resolve()), metadata.workspace_root)
        validation = prepare_validation_store.load_prepare_validation(first.plan_dir)
        assert validation is not None
        self.assertEqual(str(new_workspace.resolve()), validation.workspace_root)
        self.assertFalse((first.plan_dir / plans.PLAN_TRANSACTION_DIR_NAME).exists())
        self.assertIsNone(state.inspect_lock_info(first.plan_dir, self.state_dir))

    def test_rebind_staging_is_external_preserves_symlinks_and_rebases_paths(
        self,
    ) -> None:
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
        external_file = self.tmp_path / "operator-note.md"
        external_file.write_text("operator note\n", encoding="utf-8")
        (first.plan_dir / "operator-link.md").symlink_to(external_file)
        staged_paths: list[Path] = []

        def failing_validator(packet: prepare_packet.ValidationTaskPacket) -> object:
            staged_plan_dir = Path(packet.plan_dir)
            staged_paths.append(staged_plan_dir)
            self.assertFalse(staged_plan_dir.is_relative_to(self.base_dir))
            self.assertTrue((staged_plan_dir / "operator-link.md").is_symlink())
            raise plans.PlanError(
                f"injected staged failure at {staged_plan_dir}/session-1.md"
            )

        result = self._prepare(
            None,
            failing_validator,
            cwd=new_workspace,
            resume_plan_dir=first.plan_dir,
            rebind_workspace=True,
        )

        self.assertFalse(result.ok)
        self.assertTrue(staged_paths)
        self.assertFalse(staged_paths[0].exists())
        diagnostics = "\n".join(
            [step.detail for step in result.steps]
            + [blocker.message for blocker in result.blockers]
        )
        self.assertIn(str(first.plan_dir / "session-1.md"), diagnostics)
        self.assertNotIn(str(staged_paths[0]), diagnostics)
