"""Repository-intent intent resolution tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest

from agent_session_loop import repository_intent
from agent_session_loop.workspaces import canonicalize_filesystem_path
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_multi_repo_workspace,
)


class ResolveIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = canonicalize_filesystem_path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = canonicalize_filesystem_path(self.repositories["SVC"])
        self.second = canonicalize_filesystem_path(self.repositories["OAK"])

    def _resolve(self, *candidates, **kwargs):
        return repository_intent.resolve_intent(candidates, root=self.first, **kwargs)

    def test_two_repositories_require_a_registry(self) -> None:
        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value=str(self.second / "README.md"), surface="absolute-path"
            ),
        )

        self.assertTrue(intent.registry_required)
        self.assertEqual(
            sorted([self.first, self.second], key=str), list(intent.git_roots)
        )

    def test_one_repository_needs_no_registry(self) -> None:
        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value="src/new.py", surface="contract-path"
            ),
        )

        self.assertFalse(intent.registry_required)
        self.assertEqual((self.first,), intent.git_roots)

    def test_one_git_target_outside_a_non_git_bound_root_requires_registry(
        self,
    ) -> None:
        non_git_root = self.root / "operator-workspace"
        non_git_root.mkdir()

        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(self.first / "README.md"), surface="absolute-path"
                )
            ],
            root=non_git_root,
        )

        self.assertIsNone(intent.bound_git_root)
        self.assertEqual((self.first,), intent.git_roots)
        self.assertTrue(intent.registry_required)

    def test_same_origin_clones_stay_distinct_repositories(self) -> None:
        shared = "git@github.com:tundra/SVC.git"
        for repository in (self.first, self.second):
            subprocess.run(
                ["git", "remote", "set-url", "origin", shared],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            )

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value=str(self.second / "README.md"), surface="absolute-path"
            ),
        )

        self.assertTrue(intent.registry_required)
        self.assertEqual(
            sorted([self.first, self.second], key=str), list(intent.git_roots)
        )

    def test_a_path_that_does_not_exist_yet_names_its_repository(self) -> None:
        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.second / "src" / "deep" / "new.py"),
                surface="contract-path",
            )
        )

        self.assertEqual((self.second,), intent.git_roots)
        self.assertIsNone(intent.candidates[0].exemption)

    def test_symlinked_locations_resolve_to_the_real_git_top_level(self) -> None:
        link = self.root / "link-to-first"
        link.symlink_to(self.first, target_is_directory=True)

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(link / "README.md"), surface="absolute-path"
            )
        )

        self.assertEqual((self.first,), intent.git_roots)
        self.assertEqual(self.first / "README.md", intent.candidates[0].resolved)

    def test_non_git_artifacts_never_force_a_registry(self) -> None:
        artifact = self.root / "artifacts" / "report.json"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("{}\n", encoding="utf-8")

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value=str(artifact), surface="contract-path"
            ),
        )

        self.assertFalse(intent.registry_required)
        self.assertEqual((self.first,), intent.git_roots)
        self.assertEqual((artifact,), intent.non_git_paths)
        exemptions = {
            candidate.candidate.value: candidate.exemption
            for candidate in intent.candidates
        }
        self.assertEqual("non-git", exemptions[str(artifact)])

    def test_runner_owned_plan_artifacts_are_exempt(self) -> None:
        plan_dir = self.second / "plans" / "b766"
        plan_dir.mkdir(parents=True)

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value=str(plan_dir / "session-1.md"), surface="task-packet"
            ),
            exempt_roots=[plan_dir],
        )

        self.assertFalse(intent.registry_required)
        exemptions = {
            candidate.candidate.surface: candidate.exemption
            for candidate in intent.candidates
        }
        self.assertEqual("plan-artifact", exemptions["task-packet"])

    def test_a_cited_source_document_is_exempt_wherever_prose_names_it(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.first / "README.md"), surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value=f"{source}:209", surface="absolute-path", line=14
            ),
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        self.assertFalse(intent.registry_required)
        self.assertEqual((self.first,), intent.git_roots)
        exemptions = {
            candidate.candidate.value: candidate.exemption
            for candidate in intent.candidates
        }
        self.assertEqual("plan-provenance", exemptions[f"{source}:209"])

    def test_a_sibling_of_the_source_document_is_exempt(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        sibling = source.parent / "other-plan.md"
        sibling.write_text("# Other\n", encoding="utf-8")

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(sibling), surface="absolute-path"
            ),
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        self.assertFalse(intent.registry_required)
        self.assertEqual("plan-provenance", intent.candidates[0].exemption)

    def test_the_rest_of_the_source_repository_still_requires_a_registry(self) -> None:
        source = self.second / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")
        skill = self.second / "skills" / "foo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Skill\n", encoding="utf-8")
        settings = self.second / "settings.json"
        settings.write_text("{}\n", encoding="utf-8")

        for label, target in (("sibling directory", skill), ("repo root", settings)):
            with self.subTest(label=label):
                intent = self._resolve(
                    repository_intent.IntentCandidate(
                        value=str(target), surface="absolute-path"
                    ),
                    provenance_roots=repository_intent.provenance_exempt_roots(source),
                )

                self.assertTrue(intent.registry_required)
                self.assertEqual((self.second,), intent.git_roots)

    def test_a_deleted_source_document_is_still_exempt(self) -> None:
        source = self.second / "plans" / "deleted.md"
        source.parent.mkdir(parents=True)

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(source), surface="absolute-path"
            ),
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        self.assertFalse(intent.registry_required)
        self.assertEqual("plan-provenance", intent.candidates[0].exemption)

    def test_a_source_inside_the_bound_repository_leaves_the_verdict_alone(
        self,
    ) -> None:
        source = self.first / "plans" / "source.md"
        source.parent.mkdir(parents=True)
        source.write_text("# Source\n", encoding="utf-8")

        intent = self._resolve(
            repository_intent.IntentCandidate(
                value=str(self.second / "README.md"), surface="absolute-path"
            ),
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        self.assertTrue(intent.registry_required)
        self.assertEqual((self.second,), intent.git_roots)

    def test_a_plan_artifact_keeps_its_label_when_it_is_also_the_source(self) -> None:
        plan_dir = self.second / "plans" / "b766"
        plan_dir.mkdir(parents=True)
        source = plan_dir / "source.md"
        source.write_text("# Source\n", encoding="utf-8")

        intent = self._resolve(
            repository_intent.IntentCandidate(value=str(source), surface="task-packet"),
            exempt_roots=[plan_dir],
            provenance_roots=repository_intent.provenance_exempt_roots(source),
        )

        self.assertEqual("plan-artifact", intent.candidates[0].exemption)

    def test_system_paths_and_unresolvable_backticks_are_exempt(self) -> None:
        intent = self._resolve(
            repository_intent.IntentCandidate(
                value="/usr/bin/python3", surface="absolute-path"
            ),
            repository_intent.IntentCandidate(
                value="verify.parallel", surface="backtick-path"
            ),
        )

        exemptions = {
            candidate.candidate.value: candidate.exemption
            for candidate in intent.candidates
        }
        self.assertEqual("system-path", exemptions["/usr/bin/python3"])
        self.assertEqual("unresolved", exemptions["verify.parallel"])
        self.assertEqual((), intent.git_roots)

    def test_workspace_symlink_resolving_into_system_toolchain_is_exempt(
        self,
    ) -> None:
        tool = self.first / "venv-python"
        tool.symlink_to("/usr/bin/env")

        intent = self._resolve(
            repository_intent.IntentCandidate(value=str(tool), surface="backtick-path")
        )

        self.assertEqual("system-path", intent.candidates[0].exemption)
        self.assertEqual(
            canonicalize_filesystem_path("/usr/bin/env"),
            intent.candidates[0].resolved,
        )
        self.assertEqual((), intent.git_roots)
