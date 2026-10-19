"""Shared fixtures for test_asl_assurance_cli."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_session_loop import assurance_ledger as ledger
from agent_session_loop import (
    assurance_models_findings,
    assurance_models_inbox,
    cli,
    harnesses,
    state,
)
from agent_session_loop import assurance_projection as projection
from agent_session_loop.assurance_projection import AuditPublication

RUN_ID = "20260810T101112123456Z-4242"


OTHER_RUN_ID = "20260810T101112999999Z-4243"


THIRD_RUN_ID = "20260810T101113111111Z-4244"


TIMESTAMP = "2026-08-10T10:11:12+00:00"


LATER_TIMESTAMP = "2026-08-10T11:11:12+00:00"


LAST_TIMESTAMP = "2026-08-10T12:11:12+00:00"


INPUT_DIGEST = "a" * 64


OTHER_INPUT_DIGEST = "b" * 64


def _finding(**overrides) -> assurance_models_findings.Finding:
    payload = {
        "category": "lifecycle",
        "component": "agent_session_loop/runner.py",
        "invariant_key": "retry.budget-respected",
        "lane": "lifecycle-retry-resume",
        "severity": "high",
        "summary": "A retry ran after the budget was exhausted.",
        "recommendation": "Clamp the retry counter before relaunching.",
        "evidence": ("events.jsonl",),
    }
    payload.update(overrides)
    return assurance_models_findings.Finding(**payload)


def _artifacts(
    digest: str | None = INPUT_DIGEST,
) -> tuple[assurance_models_findings.EvidenceRef, ...]:
    return (
        assurance_models_findings.EvidenceRef(
            kind="artifact", locator="supervisor-audit-input.json", digest=digest
        ),
        assurance_models_findings.EvidenceRef(
            kind="artifact", locator="supervisor-audit.json"
        ),
    )


def _publication(**overrides) -> AuditPublication:
    payload = {
        "run_id": RUN_ID,
        "state": "findings",
        "eligible": True,
        "findings": (_finding(),),
        "artifacts": _artifacts(),
        "completed_at": TIMESTAMP,
    }
    payload.update(overrides)
    return AuditPublication(**payload)


class AssuranceCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name) / "state"
        self.ledger_path = ledger.ledger_path(self.state_dir)
        self.parser = cli.build_parser(prog="asl")

    # -- fixtures ---------------------------------------------------------

    def publish(self, publication: AuditPublication, **kwargs):
        kwargs.setdefault("recorded_at", TIMESTAMP)
        return projection.publish_audit(publication, state_dir=self.state_dir, **kwargs)

    def open_three_issues(self) -> None:
        """ASI-1 seen first, then ASI-2, then ASI-3 as the newest sighting."""
        self.publish(_publication())
        self.publish(
            _publication(
                run_id=OTHER_RUN_ID,
                findings=(_finding(invariant_key="scope.no-escape"),),
                artifacts=_artifacts(OTHER_INPUT_DIGEST),
                completed_at=LATER_TIMESTAMP,
            ),
            recorded_at=LATER_TIMESTAMP,
        )
        self.publish(
            _publication(
                run_id=THIRD_RUN_ID,
                findings=(
                    _finding(
                        invariant_key="mailbox.no-stale-read",
                        severity="low",
                        component="agent_session_loop/mailbox.py",
                    ),
                ),
                artifacts=(),
                completed_at=LAST_TIMESTAMP,
            ),
            recorded_at=LAST_TIMESTAMP,
        )

    def seed_run_directory(self, plan_name: str, run_id: str) -> Path:
        """Record one plan in the state corpus as having produced ``run_id``."""
        plan_dir = Path(self._tmp.name) / "plans" / plan_name
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_state = state.plan_state_dir(plan_dir, self.state_dir)
        (plan_state / state.RUNS_DIR_NAME / run_id).mkdir(parents=True)
        (plan_state / "state.json").write_text(
            json.dumps({"plan_dir": str(plan_dir)}), encoding="utf-8"
        )
        return plan_dir

    def inbox(self) -> projection.AssuranceProjection:
        return projection.read_projection(state_dir=self.state_dir)

    # -- invocation -------------------------------------------------------

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        args = self.parser.parse_args([*argv, "--state-dir", str(self.state_dir)])
        handler = cli.resolve_handler(args)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = handler(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_json(self, argv: list[str]) -> tuple[int, dict, str]:
        code, out, err = self.run_cli([*argv, "--json"])
        return code, json.loads(out), err

    def list_rows(self, out: str) -> list[str]:
        """Return the issue IDs an operator can copy out of the text rows."""
        return [
            line.split()[0] for line in out.splitlines() if line.startswith("  ASI-")
        ]


DRAFT_SECTIONS: dict[str, str] = {
    "Summary": "Clamp the retry counter before the relaunch decision is made.",
    "Problem and Evidence": (
        "supervisor-audit.md records a fourth attempt under a budget of three."
    ),
    "Proposed Changes": (
        "Move the budget check above the relaunch branch in runner.py."
    ),
    "Test Plan": "Assert the fourth attempt never starts once the budget is spent.",
    "Assumptions": "The recorded budget is the one the run actually started with.",
}


def _draft_document(sections: dict[str, str] | None = None) -> str:
    body = "\n\n".join(
        f"## {name}\n\n{text}"
        for name, text in (DRAFT_SECTIONS if sections is None else sections).items()
    )
    return f"# Proposal for ASI-1\n\n{body}\n"


class AssurancePlanCase(AssuranceCliTestCase):
    """Drafting a proposal, and everything drafting refuses to do."""

    def setUp(self) -> None:
        super().setUp()
        self.output = Path(self._tmp.name) / "proposal.md"

    @contextmanager
    def drafting(
        self,
        *,
        unavailable: str | None = None,
        last_message: str | None = None,
        returncode: int = 0,
        timed_out: bool = False,
        during: object = None,
    ):
        """Run drafting against a harness that answers without being installed."""
        calls: list[dict] = []
        text = _draft_document() if last_message is None else last_message

        def fake_run_harness(name, **kwargs):
            calls.append({"name": name, **kwargs})
            if during is not None:
                during()
            return harnesses.HarnessResult(
                argv=[name],
                returncode=returncode,
                stdout="",
                stderr="",
                last_message=text,
                name=name,
                timed_out=timed_out,
            )

        with (
            mock.patch.object(harnesses, "run_harness", fake_run_harness),
            (
                mock.patch.object(
                    harnesses,
                    "audit_read_only_unavailable_reason",
                    return_value=unavailable,
                )
            ),
        ):
            yield calls

    def plan_argv(self, issue: str = "ASI-1", **flags: str) -> list[str]:
        argv = [
            "assurance",
            "plan",
            issue,
            "--harness",
            "claude",
            "--output",
            str(self.output),
        ]
        for name, value in flags.items():
            argv.extend([f"--{name.replace('_', '-')}", value])
        return argv

    def drafted_plans(
        self, issue_id: str
    ) -> tuple[assurance_models_inbox.ImprovementPlanDraft, ...]:
        issue = self.inbox().issue(issue_id)
        assert issue is not None
        return issue.plans
