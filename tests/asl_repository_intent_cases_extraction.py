"""Repository-intent candidate extraction tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import authoring_render, repository_intent


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
        contract = authoring_render.contract_from_payload(
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
