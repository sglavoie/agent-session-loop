"""Repository-intent extraction, resolution, and reporting tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_session_loop import authoring, repository_intent
from agent_session_loop.workspaces import canonicalize_filesystem_path
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
)


def _surfaces(candidates) -> dict[str, str]:
    return {candidate.value: candidate.surface for candidate in candidates}


class CandidateExtractionTests(unittest.TestCase):
    def test_absolute_and_backtick_locations_carry_their_surface(self) -> None:
        text = (
            "Edit /Users/dev/work/repoA/src/main.py and `tests/support.py`.\n"
            "Run `python3 -m unittest -q` afterwards.\n"
        )

        surfaces = _surfaces(repository_intent.candidates_from_text(text))

        self.assertEqual("absolute-path", surfaces["/Users/dev/work/repoA/src/main.py"])
        self.assertEqual("backtick-path", surfaces["tests/support.py"])
        # A span holding whitespace is a command, not a location.
        self.assertNotIn("python3 -m unittest -q", surfaces)

    def test_fenced_blocks_are_illustrative_not_evidence(self) -> None:
        text = (
            "Real work in /Users/dev/work/repoA/src/main.py.\n"
            "```bash\n"
            "cd /Users/dev/work/repoB && make\n"
            "echo `/Users/dev/work/repoC/tool`\n"
            "```\n"
        )

        values = {
            candidate.value
            for candidate in repository_intent.candidates_from_text(text)
        }

        self.assertEqual({"/Users/dev/work/repoA/src/main.py"}, values)

    def test_provenance_metadata_is_where_a_plan_came_from(self) -> None:
        text = (
            "# Plan\n"
            "\n"
            "**Split from**: `/Users/dev/notes/repoB/plans/source.md`\n"
            "\n"
            "Edit /Users/dev/work/repoA/src/main.py.\n"
        )

        values = {
            candidate.value
            for candidate in repository_intent.candidates_from_plan_document(text)
        }

        self.assertEqual({"/Users/dev/work/repoA/src/main.py"}, values)
        # The unfiltered scanner still sees it, so the filter is doing the work.
        self.assertIn(
            "/Users/dev/notes/repoB/plans/source.md",
            {
                candidate.value
                for candidate in repository_intent.candidates_from_text(text)
            },
        )

    def test_provenance_filtering_preserves_line_numbers(self) -> None:
        text = (
            "# Plan\n"
            "\n"
            "- **Split from**: `session-1.md`\n"
            "\n"
            "Edit /Users/dev/work/repoA/src/main.py.\n"
        )

        candidates = repository_intent.candidates_from_plan_document(text)

        self.assertEqual(
            [("/Users/dev/work/repoA/src/main.py", 5)],
            [(candidate.value, candidate.line) for candidate in candidates],
        )

    def test_documents_without_provenance_are_scanned_unchanged(self) -> None:
        text = "Edit /Users/dev/work/repoA/src/main.py and `tests/support.py`.\n"

        self.assertEqual(
            repository_intent.candidates_from_text(text),
            repository_intent.candidates_from_plan_document(text),
        )

    def test_contract_checks_yield_path_and_cwd_surfaces(self) -> None:
        contract = authoring.contract_from_payload(
            "1",
            ["done"],
            {
                "session": "1",
                "checks": [
                    {"name": "a", "type": "file_exists", "path": "src/new.py"},
                    {
                        "name": "b",
                        "type": "command",
                        "argv": ["true"],
                        "cwd": "/Users/dev/work/repoB",
                    },
                ],
            },
        )

        surfaces = _surfaces(
            repository_intent.candidates_from_checks(contract.verify.checks)
        )

        self.assertEqual("contract-path", surfaces["src/new.py"])
        self.assertEqual("contract-cwd", surfaces["/Users/dev/work/repoB"])

    def test_raw_contract_payloads_are_read_without_parsing(self) -> None:
        payload = {
            "1": {
                "done_when": ["done"],
                "verify": {
                    "checks": [
                        {"type": "file_exists", "path": "/w/repoA/new.py"},
                        {"type": "command", "cwd": "/w/repoB"},
                    ]
                },
            },
            "2": "not-an-object",
        }

        surfaces = _surfaces(
            repository_intent.candidates_from_contract_payloads(payload)
        )

        self.assertEqual("contract-path", surfaces["/w/repoA/new.py"])
        self.assertEqual("contract-cwd", surfaces["/w/repoB"])

    def test_plan_metadata_execution_roots_and_task_packets(self) -> None:
        metadata = {
            "cwd": "/w/repoA",
            "workspace_root": "/w",
            "repositories": [{"root": "/w/repoB"}, {"path": "/w/repoC"}],
        }

        metadata_surfaces = _surfaces(
            repository_intent.candidates_from_plan_metadata(metadata)
        )
        execution_surfaces = _surfaces(
            repository_intent.candidates_from_execution_roots(["/w/repoA"])
        )
        packet_surfaces = _surfaces(
            repository_intent.candidates_from_task_packet(
                [Path("/plans/p/session-1.md")]
            )
        )

        self.assertEqual(
            {"/w/repoA", "/w", "/w/repoB", "/w/repoC"},
            set(metadata_surfaces),
        )
        self.assertEqual({"plan-metadata"}, set(metadata_surfaces.values()))
        self.assertEqual("execution-root", execution_surfaces["/w/repoA"])
        self.assertEqual("task-packet", packet_surfaces["/plans/p/session-1.md"])


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


class IntentReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = canonicalize_filesystem_path(self._temp.name)
        self.repository = self.root / "repo"
        init_git_repo(self.repository)

    def test_report_identifies_surface_resolution_root_and_exemption(
        self,
    ) -> None:
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(self.repository / "README.md"),
                    surface="absolute-path",
                    line=7,
                )
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)

        self.assertFalse(report["registry_required"])
        self.assertIsNone(report["reason"])
        self.assertEqual([str(self.repository)], report["git_roots"])
        entry = report["candidates"][0]
        self.assertEqual("absolute-path", entry["surface"])
        self.assertEqual(7, entry["line"])
        self.assertEqual(str(self.repository / "README.md"), entry["resolved"])
        self.assertEqual(str(self.repository), entry["git_root"])
        self.assertIsNone(entry["exemption"])

    def test_report_names_the_shared_reason_when_a_registry_is_required(
        self,
    ) -> None:
        second = self.root / "other"
        init_git_repo(second)
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value=str(self.repository / "README.md"),
                    surface="absolute-path",
                ),
                repository_intent.IntentCandidate(
                    value=str(second / "README.md"), surface="absolute-path"
                ),
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)

        self.assertTrue(report["registry_required"])
        self.assertEqual(
            repository_intent.REASON_REPOSITORY_REGISTRY_REQUIRED,
            report["reason"],
        )

    def test_report_is_bounded(self) -> None:
        candidates = [
            repository_intent.IntentCandidate(
                value=f"file-{index}.txt", surface="contract-path"
            )
            for index in range(12)
        ]
        intent = repository_intent.resolve_intent(candidates, root=self.repository)

        report = repository_intent.intent_report(intent, limit=5)

        self.assertEqual(5, len(report["candidates"]))
        self.assertEqual(12, report["candidate_count"])
        self.assertEqual(7, report["omitted_candidates"])

    def test_report_redacts_credentials(self) -> None:
        intent = repository_intent.resolve_intent(
            [
                repository_intent.IntentCandidate(
                    value="https://alice:s3cret@github.example/org/repo.git",
                    surface="plan-metadata",
                )
            ],
            root=self.repository,
        )

        report = repository_intent.intent_report(intent)
        rendered = repr(report)

        self.assertNotIn("s3cret", rendered)
        self.assertIn("***@", report["candidates"][0]["text"])


class PlanIntentTests(unittest.TestCase):
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

    def test_source_only_evidence_can_be_conclusive(self) -> None:
        text = f"Edit {self.first}/README.md.\nThen add {self.second}/src/new.py.\n"

        intent = repository_intent.plan_intent(text, root=self.first)

        self.assertTrue(intent.registry_required)

    def test_contracts_add_evidence_the_prose_never_carries(self) -> None:
        text = "Do the first thing.\nThen do the second.\n"
        contracts_by_id = {
            "1": {
                "verify": {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": str(self.first / "README.md"),
                        }
                    ]
                }
            },
            "2": {
                "verify": {
                    "checks": [
                        {
                            "type": "file_exists",
                            "path": str(self.second / "README.md"),
                        }
                    ]
                }
            },
        }

        self.assertFalse(
            repository_intent.plan_intent(text, root=self.first).registry_required
        )
        self.assertTrue(
            repository_intent.plan_intent(
                text, root=self.first, contracts_by_id=contracts_by_id
            ).registry_required
        )


if __name__ == "__main__":
    unittest.main()
