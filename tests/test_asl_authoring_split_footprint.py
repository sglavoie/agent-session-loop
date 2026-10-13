"""Split repository footprint tests.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_session_loop import (
    authoring_source_models,
    authoring_split,
    authoring_split_cli,
    workspaces,
)
from tests.support import (
    DEFAULT_MULTI_REPO_SPECS,
    init_multi_repo_workspace,
)


class SplitRepositoryFootprintTests(unittest.TestCase):
    """`asl split` refuses to write a multi-repository plan without a registry."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.workspace = self.root / "workspace"
        self.repositories = init_multi_repo_workspace(
            self.workspace, DEFAULT_MULTI_REPO_SPECS[:2]
        )
        self.first = self.repositories["SVC"]
        self.second = self.repositories["OAK"]
        for repository in (self.first, self.second):
            (repository / "README.md").write_text("x\n", encoding="utf-8")

    def _write_plan(self, body: str) -> Path:
        plan_file = self.root / "plan.md"
        plan_file.write_text(body, encoding="utf-8")
        return plan_file

    def _canonical_roots(self) -> list[str]:
        return sorted(
            str(workspaces.canonicalize_filesystem_path(repository))
            for repository in (self.first, self.second)
        )

    def _check(self, plan_file: Path, cwd: Path) -> dict:
        args = argparse.Namespace(
            json=True, inline_stdin=False, plan_file=str(plan_file), cwd=str(cwd)
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            authoring_split_cli.check_from_args(args)
        return json.loads(buffer.getvalue())

    def _write_contracts(self, contracts_by_id: dict) -> Path:
        contracts_file = self.root / "contracts.json"
        contracts_file.write_text(json.dumps(contracts_by_id), encoding="utf-8")
        return contracts_file

    def _split(
        self, plan_file: Path, contracts_file: Path, cwd: Path, *, out: str = "out"
    ) -> tuple[int, dict, Path]:
        output_base = self.root / out
        args = argparse.Namespace(
            json=True,
            inline_stdin=False,
            plan_file=str(plan_file),
            base_dir=str(output_base),
            check=False,
            contracts_file=str(contracts_file),
            skill_prefix="claude",
            analysis_file=None,
            cwd=str(cwd),
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = authoring_split_cli.split_from_args(args)
        return exit_code, json.loads(buffer.getvalue()), output_base

    def test_multi_repository_plan_without_a_registry_is_refused(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then update {self.second}/README.md too.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertEqual(
            authoring_split.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"]
        )
        self.assertEqual(
            [authoring_source_models.LINT_MISSING_REPOSITORY_REGISTRY],
            [finding["category"] for finding in payload["findings"]],
        )
        intent = payload["repository_intent"]
        self.assertTrue(intent["registry_required"])
        self.assertEqual(self._canonical_roots(), sorted(intent["git_roots"]))
        footprint = payload["repository_footprint"]
        self.assertFalse(footprint["registry_present"])
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(entry["alias"] for entry in footprint["detected"]),
        )
        self.assertEqual(
            ["| Alias | Path | Origin |", "| --- | --- | --- |"],
            footprint["suggested_registry_table"][:2],
        )

    def test_single_repository_plan_still_checks_clean(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Revisit {self.first}/README.md once more.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_footprint", payload)

    def test_a_plan_citing_its_own_path_is_not_a_second_repository(self) -> None:
        # No plan directory exists yet at `--check` time, so the source
        # document can only be exempted as the source document.
        plan_file = self.second / "plans" / "plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"The wording comes from {plan_file}:14.\n",
            encoding="utf-8",
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_footprint", payload)
        self.assertNotIn("repository_intent", payload)

    def test_a_plan_reaching_elsewhere_in_its_own_repository_is_refused(self) -> None:
        plan_file = self.second / "plans" / "plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then update {self.second}/README.md too.\n",
            encoding="utf-8",
        )

        payload = self._check(plan_file, self.first)

        self.assertEqual(
            authoring_split.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"]
        )
        self.assertEqual(
            self._canonical_roots(),
            sorted(payload["repository_intent"]["git_roots"]),
        )

    def test_a_non_git_path_outside_the_root_still_checks_clean(self) -> None:
        loose = self.root / "loose.txt"
        loose.write_text("x\n", encoding="utf-8")
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then read {loose} for reference.\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_footprint", payload)

    def test_paths_inside_a_fenced_block_are_not_evidence(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            "```bash\n"
            f"cd {self.second} && make\n"
            "```\n"
        )

        payload = self._check(plan_file, self.first)

        self.assertTrue(payload["ok"])

    def test_write_path_refuses_contracts_that_span_repositories(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\nDo the first thing.\n\n"
            "## Session 2: Second\n\nDo the second thing.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "path": str(self.second / "README.md"),
                            }
                        ],
                    },
                },
            }
        )

        # The prose alone names no repository, so `--check` has nothing
        # conclusive to report and defers to the contract-aware write path.
        self.assertTrue(self._check(plan_file, self.first)["ok"])

        exit_code, payload, output_base = self._split(
            plan_file, contracts_file, self.first
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(
            authoring_split.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"]
        )
        self.assertEqual(
            self._canonical_roots(),
            sorted(payload["repository_intent"]["git_roots"]),
        )
        self.assertEqual(
            ["tundra_OAK", "tundra_svc"],
            sorted(
                entry["alias"] for entry in payload["repository_footprint"]["detected"]
            ),
        )
        self.assertFalse(output_base.exists())

    def test_contract_target_that_does_not_exist_yet_still_names_its_repository(
        self,
    ) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Work in {self.first}/README.md.\n\n"
            "## Session 2: Second\n\nDo the second thing.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "path": str(self.second / "src" / "new.py"),
                            }
                        ],
                    },
                },
            }
        )

        exit_code, payload, output_base = self._split(
            plan_file, contracts_file, self.first
        )

        self.assertEqual(1, exit_code)
        self.assertEqual(
            authoring_split.SPLIT_REASON_REGISTRY_REQUIRED, payload["reason"]
        )
        self.assertEqual(
            self._canonical_roots(),
            sorted(payload["repository_intent"]["git_roots"]),
        )
        self.assertFalse(output_base.exists())

    def test_single_repository_legacy_plan_still_splits(self) -> None:
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Session 1: First\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            f"Then add {self.first}/src/new.py.\n"
        )
        contracts_file = self._write_contracts(
            {
                session_id: {
                    "done_when": ["done"],
                    "verify": {
                        "session": session_id,
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "path": str(self.first / "README.md"),
                            }
                        ],
                    },
                }
                for session_id in ("1", "2")
            }
        )

        exit_code, payload, _ = self._split(plan_file, contracts_file, self.first)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertNotIn("repository_intent", payload)

    def test_registered_multi_repository_plan_still_splits(self) -> None:
        rows = "\n".join(
            f"| {alias} | {path} | {origin} |"
            for alias, path, origin in DEFAULT_MULTI_REPO_SPECS[:2]
        )
        plan_file = self._write_plan(
            "# Plan\n\n"
            "## Repositories\n\n"
            "| Alias | Path | Origin |\n"
            "| --- | --- | --- |\n"
            f"{rows}\n\n"
            "## Session 1: First\n\n"
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: SVC\n\n"
            f"Edit {self.first}/README.md carefully.\n\n"
            "## Session 2: Second\n\n"
            "**Reads repositories**: SVC, OAK\n"
            "**Writes repositories**: OAK\n\n"
            f"Then update {self.second}/README.md too.\n"
        )
        contracts_file = self._write_contracts(
            {
                "1": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "1",
                        "checks": [
                            {
                                "name": "a",
                                "type": "file_exists",
                                "repo": "SVC",
                                "path": "README.md",
                            }
                        ],
                    },
                },
                "2": {
                    "done_when": ["done"],
                    "verify": {
                        "session": "2",
                        "checks": [
                            {
                                "name": "b",
                                "type": "file_exists",
                                "repo": "OAK",
                                "path": "README.md",
                            }
                        ],
                    },
                },
            }
        )

        exit_code, payload, _ = self._split(plan_file, contracts_file, self.workspace)

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            ["SVC", "OAK"],
            [repository["alias"] for repository in payload["repositories"]],
        )


if __name__ == "__main__":
    unittest.main()
