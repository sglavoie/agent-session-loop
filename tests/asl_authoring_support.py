"""Shared fixtures for the plan authoring test modules.

Split from tests/test_asl_authoring.py."""

from __future__ import annotations

import unittest
from pathlib import Path

from agent_session_loop import (
    authoring_manifest,
    authoring_render,
    authoring_transaction,
    contracts,
    plans_models,
    plans_session_ids,
)


class PlanAuthoringBase(unittest.TestCase):
    def _command_contract(
        self, session_id: str, *, done: str = "Tests pass."
    ) -> contracts.CompletionContract:
        return contracts.CompletionContract(
            session_id=plans_session_ids.parse_session_id(session_id),
            done_when=(done,),
            verify=contracts.VerifyRecipe(
                checks=(
                    contracts.CommandCheck(
                        name="c1",
                        argv=("python3", "-m", "unittest", "-q"),
                        timeout_seconds=60.0,
                    ),
                )
            ),
        )

    @staticmethod
    def _inline_contract_lines(check_name: str) -> list[str]:
        return [
            "## Completion Contract",
            "",
            "### Done when",
            "",
            "- Inline criterion.",
            "",
            "### Verify",
            "",
            "```json",
            "{",
            '  "session": "1",',
            '  "checks": [',
            "    {",
            f'      "name": "{check_name}",',
            '      "type": "command",',
            '      "argv": ["python3", "-c", "pass"]',
            "    }",
            "  ]",
            "}",
            "```",
        ]

    def _manifest_spec(self) -> authoring_manifest.ManifestSpec:
        sessions = (
            authoring_manifest.ManifestSession(
                session_id=plans_session_ids.parse_session_id("1"), title="First"
            ),
            authoring_manifest.ManifestSession(
                session_id=plans_session_ids.parse_session_id("2"), title="Second"
            ),
        )
        return authoring_manifest.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=sessions,
            split_date="2026-07-10",
            readiness=plans_models.ReadinessMetadata(
                state="draft",
                readiness_sha256="d" * 64,
                checked_at="2026-07-10T00:00:00+00:00",
            ),
            skill_prefix="/",
        )

    def _author_plan(self, plan_dir: Path) -> Path:
        """Author a two-session plan directory deterministically; return its path."""
        spec = authoring_manifest.ManifestSpec(
            plan_title="My Plan",
            source_path="/plans/PLAN.md",
            output_dir="/plans/out",
            plan_id="aaaa-bbbb",
            source_fingerprint="f" * 64,
            content_sha256="c" * 64,
            sessions=(
                authoring_manifest.ManifestSession(
                    session_id=plans_session_ids.parse_session_id("1"), title="First"
                ),
                authoring_manifest.ManifestSession(
                    session_id=plans_session_ids.parse_session_id("2"), title="Second"
                ),
            ),
            split_date="2026-07-10",
            readiness=plans_models.ReadinessMetadata(
                state="draft", readiness_sha256="0" * 64, checked_at="pending"
            ),
            skill_prefix="/",
        )
        tx = authoring_transaction.PlanTransaction(plan_dir)
        tx.stage("manifest.md", authoring_manifest.render_manifest(spec))
        tx.stage(
            "context.md",
            authoring_render.render_context(["# My Plan", "", "ctx"], "/plans/PLAN.md"),
        )
        tx.stage("progress.md", authoring_render.render_progress_skeleton())
        for sid, title in (("1", "First"), ("2", "Second")):
            tx.stage(
                f"session-{sid}.md",
                authoring_render.render_session_file(
                    session_id=sid,
                    title=title,
                    instruction_lines=["Do the work."],
                    is_first=(sid == "1"),
                    previous_session_id=("1" if sid == "2" else None),
                    contract=self._command_contract(sid),
                ),
            )
        tx.commit()
        return plan_dir
