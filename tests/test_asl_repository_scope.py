"""Repository-footprint extraction, resolution, and suggestion tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_session_loop import (
    contracts,
    repository_scope,
    repository_scope_payload,
    repository_scope_registry,
    repository_scope_targets,
)
from agent_session_loop.workspaces import (
    WorkspaceError,
    canonicalize_filesystem_path,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_git_repo,
    init_multi_repo_workspace,
)


def _values(targets: tuple[repository_scope_targets.PathTarget, ...]) -> list[str]:
    return [target.value for target in targets]


class ExtractPathTargetsTests(unittest.TestCase):
    def test_absolute_and_home_paths_are_hard_and_prose_is_soft(self) -> None:
        text = (
            "Edit /Users/dev/work/repoA/src/main.py and ~/scripts/tool.py.\n"
            "Also touch src/main.py somewhere.\n"
        )
        targets = repository_scope_targets.extract_path_targets(text)
        by_confidence = {target.value: target.confidence for target in targets}
        self.assertEqual("hard", by_confidence["/Users/dev/work/repoA/src/main.py"])
        self.assertEqual("hard", by_confidence["~/scripts/tool.py"])
        self.assertEqual("soft", by_confidence["src/main.py"])

    def test_trailing_sentence_punctuation_is_not_part_of_the_path(self) -> None:
        targets = repository_scope_targets.extract_path_targets(
            "It lives at /Users/dev/work/repoA/src/main.py.\n"
        )
        self.assertEqual(["/Users/dev/work/repoA/src/main.py"], _values(targets))

    def test_fenced_blocks_are_not_evidence(self) -> None:
        text = (
            "Prose mentions nothing.\n```bash\ncd /Users/dev/work/repoB && make\n```\n"
        )
        self.assertEqual((), repository_scope_targets.extract_path_targets(text))

    def test_inline_backticks_are_still_evidence(self) -> None:
        targets = repository_scope_targets.extract_path_targets(
            "Update `/Users/dev/work/repoA/src/main.py` today.\n"
        )
        self.assertEqual(["/Users/dev/work/repoA/src/main.py"], _values(targets))

    def test_qualified_handoff_entries_are_hard_targets(self) -> None:
        targets = repository_scope_targets.extract_path_targets("- Kiln:src/app.ts\n")
        self.assertEqual(["Kiln:src/app.ts"], _values(targets))
        self.assertEqual("handoff", targets[0].kind)
        self.assertEqual("hard", targets[0].confidence)

    def test_urls_are_not_mistaken_for_handoff_entries(self) -> None:
        targets = repository_scope_targets.extract_path_targets(
            "See https://example.com/docs/guide for details.\n"
        )
        self.assertNotIn("handoff", [target.kind for target in targets])

    def test_bare_root_level_tokens_are_not_paths(self) -> None:
        self.assertEqual(
            (), repository_scope_targets.extract_path_targets("Mount /opt today.\n")
        )

    def test_contract_checks_become_hard_targets(self) -> None:
        checks = (
            contracts.FileExistsCheck(name="a", path="src/created.py"),
            contracts.RegexInFileCheck(
                name="b", path="/absolute/elsewhere.py", pattern="x"
            ),
            contracts.CommandCheck(
                name="c", argv=("true",), timeout_seconds=1.0, cwd="pkg"
            ),
            contracts.CommandCheck(name="d", argv=("true",), timeout_seconds=1.0),
        )
        targets = repository_scope_targets.targets_from_checks(checks)
        self.assertEqual(
            ["src/created.py", "/absolute/elsewhere.py", "pkg"], _values(targets)
        )
        self.assertEqual(
            ["contract-path", "contract-path", "contract-cwd"],
            [target.kind for target in targets],
        )
        self.assertTrue(all(target.confidence == "hard" for target in targets))


class ResolveFootprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name) / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "src").mkdir()
            (repository / "src" / "main.py").write_text("x\n", encoding="utf-8")

    def _footprint(self, text: str, root: Path) -> repository_scope.RepositoryFootprint:
        return repository_scope.resolve_footprint(
            repository_scope_targets.extract_path_targets(text), root
        )

    def test_single_repository_plan_does_not_require_a_registry(self) -> None:
        footprint = self._footprint(
            f"Edit {self.first}/src/main.py today.\n", self.first
        )
        self.assertEqual(
            (canonicalize_filesystem_path(self.first),), footprint.repo_roots
        )
        self.assertEqual((), footprint.out_of_root)
        self.assertFalse(footprint.registry_required)

    def test_two_repositories_require_a_registry(self) -> None:
        footprint = self._footprint(
            f"Edit {self.first}/src/main.py and {self.second}/src/main.py.\n",
            self.first,
        )
        self.assertEqual(
            sorted(
                (
                    canonicalize_filesystem_path(self.first),
                    canonicalize_filesystem_path(self.second),
                ),
                key=str,
            ),
            sorted(footprint.repo_roots, key=str),
        )
        self.assertTrue(footprint.registry_required)

    def test_git_owned_out_of_root_target_requires_a_registry_on_its_own(self) -> None:
        outside = self.second / "src" / "main.py"
        footprint = self._footprint(f"Check {outside} exists.\n", self.first)
        self.assertEqual([str(outside)], _values(footprint.out_of_root))
        self.assertEqual((), footprint.external)
        self.assertTrue(footprint.registry_required)

    def test_non_git_escape_is_external_and_requires_no_registry(self) -> None:
        outside = Path(self._temp.name) / "loose.txt"
        outside.write_text("x\n", encoding="utf-8")
        footprint = self._footprint(f"Check {outside} exists.\n", self.first)
        self.assertEqual([str(outside)], _values(footprint.external))
        self.assertEqual((), footprint.out_of_root)
        self.assertFalse(footprint.registry_required)

    def test_target_above_the_bound_root_inside_the_repository_still_blocks(
        self,
    ) -> None:
        footprint = self._footprint(
            f"Read {self.first}/README.md first.\n", self.first / "src"
        )
        self.assertEqual([f"{self.first}/README.md"], _values(footprint.out_of_root))
        self.assertEqual((), footprint.external)
        self.assertTrue(footprint.registry_required)

    def test_absolute_target_inside_the_root_stays_clean(self) -> None:
        footprint = repository_scope.resolve_footprint(
            (
                repository_scope_targets.PathTarget(
                    value=str(self.first / "src" / "main.py"),
                    kind="contract-path",
                    line=0,
                    confidence="hard",
                ),
            ),
            self.first,
        )
        self.assertEqual((), footprint.out_of_root)
        self.assertFalse(footprint.registry_required)

    def test_nonexistent_paths_are_unresolved_and_never_fabricate_a_repository(
        self,
    ) -> None:
        footprint = self._footprint(
            f"Create {self.first}/src/brand/new.py in this session.\n", self.first
        )
        self.assertEqual(
            [f"{self.first}/src/brand/new.py"], _values(footprint.unresolved)
        )
        self.assertEqual((), footprint.repo_roots)
        self.assertFalse(footprint.registry_required)

    def test_system_paths_are_ignored(self) -> None:
        footprint = self._footprint(
            "Run /usr/bin/python3 and read /etc/hosts first.\n", self.first
        )
        self.assertEqual((), footprint.out_of_root)
        self.assertEqual((), footprint.unresolved)
        self.assertFalse(footprint.registry_required)

    def test_soft_prose_present_in_two_repositories_is_ambiguous(self) -> None:
        footprint = self._footprint(
            f"Edit {self.first}/README.md and {self.second}/README.md, "
            "then revisit src/main.py.\n",
            self.workspace,
        )
        self.assertEqual(
            ["src/main.py"],
            [entry.target.value for entry in footprint.ambiguous],
        )
        self.assertEqual(
            sorted(
                (
                    canonicalize_filesystem_path(self.first),
                    canonicalize_filesystem_path(self.second),
                ),
                key=str,
            ),
            sorted(footprint.ambiguous[0].candidates, key=str),
        )

    def test_soft_prose_never_requires_a_registry_by_itself(self) -> None:
        footprint = self._footprint(
            "Revisit src/main.py in this session.\n", self.first
        )
        self.assertFalse(footprint.registry_required)


class SuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = Path(self._temp.name) / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )

    def test_workspace_root_is_the_shallowest_common_ancestor(self) -> None:
        roots = tuple(self.repositories.values())
        self.assertEqual(
            canonicalize_filesystem_path(self.workspace),
            repository_scope_registry.suggest_workspace_root(roots),
        )

    def test_nested_repositories_have_no_suggested_workspace_root(self) -> None:
        outer = self.repositories["SVC"]
        inner = outer / "vendor"
        init_multi_repo_workspace(inner, (("X", "inner", "git@host:x/y.git"),))
        self.assertIsNone(
            repository_scope_registry.suggest_workspace_root((outer, inner / "inner"))
        )

    def test_registry_is_built_from_real_paths_and_origins(self) -> None:
        registry = repository_scope_registry.suggest_registry(
            self.workspace, tuple(self.repositories.values())
        )
        self.assertEqual(
            [
                ("tundra_OAK", "github.com/tundra/OAK"),
                ("tundra_svc", "github.com/tundra/SVC"),
            ],
            sorted((entry.path, entry.origin) for entry in registry),
        )
        self.assertEqual(
            ["| Alias | Path | Origin |", "| --- | --- | --- |"],
            list(repository_scope_registry.registry_table_lines(registry))[:2],
        )

    def test_aliases_differing_only_in_case_are_made_distinct(self) -> None:
        repositories = init_multi_repo_workspace(
            self.workspace / "cased",
            specs=(
                ("A", "a/svc", "git@github.com:tundra/a.git"),
                ("B", "b/SVC", "git@github.com:tundra/b.git"),
            ),
        )

        registry = repository_scope_registry.suggest_registry(
            self.workspace / "cased", tuple(repositories.values())
        )

        self.assertEqual(("svc", "SVC_2"), registry.aliases)

    def test_a_repository_without_an_origin_still_yields_a_row(self) -> None:
        local = self.workspace / "local_only"
        init_git_repo(local)

        suggestions = repository_scope_registry.suggest_repositories(
            self.workspace, (self.repositories["SVC"], local)
        )

        by_alias = {entry.alias: entry.origin for entry in suggestions}
        self.assertIsNone(by_alias["local_only"])
        self.assertIn(
            f"| local_only | local_only | "
            f"{repository_scope_registry.UNKNOWN_ORIGIN_PLACEHOLDER} |",
            repository_scope_registry.registry_table_lines(suggestions),
        )
        with self.assertRaises(WorkspaceError):
            repository_scope_registry.suggest_registry(
                self.workspace, (self.repositories["SVC"], local)
            )

    def test_payload_survives_repositories_without_origins(self) -> None:
        first = self.workspace / "left"
        second = self.workspace / "right"
        for repository in (first, second):
            init_git_repo(repository)
        targets = tuple(
            repository_scope_targets.PathTarget(
                value=str(repository / "README.md"),
                kind="contract-path",
                line=0,
                confidence="hard",
            )
            for repository in (first, second)
        )
        footprint = repository_scope.resolve_footprint(targets, self.workspace)

        payload = repository_scope_payload.footprint_payload(
            {"1": targets}, footprint, registry_present=False
        )

        assert payload is not None
        self.assertEqual(
            ["left", "right"],
            sorted(str(entry["alias"]) for entry in payload["detected"]),
        )
        self.assertEqual(["left", "right"], payload["suggested_scopes"]["1"]["writes"])

    def test_payload_reports_a_non_git_escape_beside_out_of_root(self) -> None:
        loose = Path(self._temp.name) / "loose.txt"
        loose.write_text("x\n", encoding="utf-8")
        targets = tuple(
            repository_scope_targets.PathTarget(
                value=str(value),
                kind="contract-path",
                line=0,
                confidence="hard",
            )
            for value in (
                self.repositories["SVC"] / "README.md",
                self.repositories["OAK"] / "README.md",
                loose,
            )
        )
        footprint = repository_scope.resolve_footprint(targets, self.workspace)

        payload = repository_scope_payload.footprint_payload(
            {"1": targets}, footprint, registry_present=False
        )

        assert payload is not None
        self.assertEqual([], payload["out_of_root"])
        self.assertEqual([str(loose)], [entry["text"] for entry in payload["external"]])

    def test_registry_refuses_a_repository_outside_the_workspace(self) -> None:
        stray = Path(self._temp.name) / "stray"
        init_multi_repo_workspace(stray, (("Y", "loose", "git@host:y/z.git"),))
        with self.assertRaises(WorkspaceError):
            repository_scope_registry.suggest_registry(
                self.workspace, (stray / "loose",)
            )

    def test_aliases_are_deduplicated_deterministically(self) -> None:
        nested = self.workspace / "vendor"
        init_multi_repo_workspace(nested, (("Z", "tundra_svc", "git@host:z/dup.git"),))
        registry = repository_scope_registry.suggest_registry(
            self.workspace,
            (self.repositories["SVC"], nested / "tundra_svc"),
        )
        self.assertEqual(
            ["tundra_svc", "tundra_svc_2"],
            [entry.alias for entry in registry],
        )

    def test_scopes_separate_mentioned_reads_from_contract_writes(self) -> None:
        first = self.repositories["SVC"]
        second = self.repositories["OAK"]
        (first / "src").mkdir()
        (first / "src" / "main.py").write_text("x\n", encoding="utf-8")
        registry = repository_scope_registry.suggest_registry(
            self.workspace, (first, second)
        )
        alias_by_root = {
            canonicalize_filesystem_path(self.workspace / entry.path): entry.alias
            for entry in registry
        }
        targets = (
            repository_scope_targets.PathTarget(
                value=str(first / "src" / "main.py"),
                kind="contract-path",
                line=0,
                confidence="hard",
            ),
            repository_scope_targets.PathTarget(
                value=str(second / "README.md"),
                kind="prose",
                line=4,
                confidence="hard",
            ),
        )
        scopes = repository_scope_registry.suggest_scopes(
            {"1": targets}, root=self.workspace, alias_by_root=alias_by_root
        )
        self.assertEqual(("tundra_svc", "tundra_OAK"), scopes["1"].reads)
        self.assertEqual(("tundra_svc",), scopes["1"].writes)


if __name__ == "__main__":
    unittest.main()
