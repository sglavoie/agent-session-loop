"""Synthetic evidence and evaluator fixtures for worker verification tests."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_session_loop import state
from tests.support import REPO_ROOT

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "asl_worker_verification"

FAKE_WORKER = FIXTURE_DIR / "fake_worker.py"


def _load_evaluator() -> Any:
    """Import the evaluator by path.

    It is a fixture rather than a package module, and ``tests/fixtures`` is not
    importable, so the path load is what the fixture's own executables do too.
    """
    spec = importlib.util.spec_from_file_location(
        "asl_worker_verification_evaluate", FIXTURE_DIR / "evaluate.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluate = _load_evaluator()

# --------------------------------------------------------------------------
# Synthetic evidence
# --------------------------------------------------------------------------


def _invocation(
    *,
    check: str,
    actor: str,
    exit_code: int = 0,
    started_at: str = "2026-09-12T00:00:00+00:00",
    conversation: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "check": check,
        "argv": ["checks/contract.py"],
        "cwd": "/repo",
        "started_at": started_at,
        "finished_at": started_at,
        "duration_seconds": 0.01,
        "exit_code": exit_code,
        "detail": None,
        "pid": 1,
        "ppid": 2,
        "session_leader": actor == "runner",
        "actor": actor,
        "declared_role": actor if actor == "evaluator" else None,
        "worker_env": actor == "worker",
        "worker_conversation_id": conversation,
    }


def _gate_event(*, status: str = "pass") -> dict[str, Any]:
    return {
        "created_at": "2026-09-12T00:00:01+00:00",
        "name": "verification_check_finished",
        "payload": {
            "kind": "command",
            "name": evaluate.CONTRACT_CHECK_NAME,
            "returncode": 0 if status == "pass" else 1,
            "role": "gate",
            "status": status,
            "session_id": "1",
        },
        "version": 2,
    }


def _conversation_record(native_id: str) -> dict[str, Any]:
    return {
        "kind": "conversation_opened",
        "recorded_at": "2026-09-12T00:00:00+00:00",
        "reference": {
            "access_profile": "worker",
            "harness": "claude",
            "native_id": native_id,
            "session_id": "1",
            "version": 1,
        },
        "version": 1,
    }


class SyntheticTrial:
    """A trial's artifacts on disk, without ever having run one.

    Written through the same paths ``load_evidence`` reads, so a test that
    changes one fact changes exactly the fact a real trial would have changed.
    """

    def __init__(
        self,
        root: Path,
        *,
        scenario: str,
        trial: int,
        invocations: list[dict[str, Any]],
        events: list[dict[str, Any]],
        conversations: list[str],
        handoff_status: str = "complete",
        handoff_notes: str = "",
        final_state: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        self.trial_dir = root / "trials" / scenario / str(trial)
        workspace = self.trial_dir / "workspace"
        plan_dir = workspace / "plan"
        state_dir = workspace / "state"
        repo = workspace / "repo"
        for path in (plan_dir, state_dir, repo):
            path.mkdir(parents=True, exist_ok=True)
        plan_state = state.plan_state_dir(plan_dir, state_dir)
        plan_state.mkdir(parents=True, exist_ok=True)

        log_path = workspace / "invocations.jsonl"
        log_path.write_text(
            "".join(json.dumps(row) + "\n" for row in invocations), encoding="utf-8"
        )
        (plan_state / "events.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in events), encoding="utf-8"
        )
        (plan_state / "conversations.jsonl").write_text(
            "".join(
                json.dumps(_conversation_record(native)) + "\n"
                for native in conversations
            ),
            encoding="utf-8",
        )
        (plan_dir / "session-1.md").write_text(
            "\n".join(
                [
                    "# Session 1: Synthetic",
                    "",
                    "## Handoff",
                    "",
                    f"- **Status**: {handoff_status}",
                    "- **Files modified**: toolkit.py",
                    "- **Key decisions**: (none)",
                    "- **Blockers for next session**: (none)",
                    f"- **Notes**: {handoff_notes}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        scenario_object = evaluate.SCENARIOS_BY_KEY[scenario]
        receipt: dict[str, Any] = {
            "schema": evaluate.RECEIPT_VERSION,
            "scenario": scenario,
            "case": scenario_object.case,
            "trial": trial,
            "created_at": "2026-09-12T00:00:02+00:00",
            "live": True,
            "harness": "claude",
            "model_requested": "claude-opus-5",
            "candidate": {"root": str(REPO_ROOT), "commit": "0" * 40, "dirty": False},
            "policy_surfaces": evaluate.policy_surfaces(REPO_ROOT),
            "fixture_digest": evaluate.fixture_digest(),
            "workspace": {
                "root": str(workspace),
                "repo": str(repo),
                "plan_dir": str(plan_dir),
                "state_dir": str(state_dir),
                "invocations": str(log_path),
            },
            "run": {"argv": ["asl", "run"], "returncode": 0, "timed_out": False},
            "final_state": final_state
            if final_state is not None
            else {
                "focused_exit": 0,
                "contract_exit": 0,
                "changed_paths": ["toolkit.py"],
                "head": "1" * 40,
            },
        }
        receipt["policy_digest"] = evaluate.policy_digest(receipt["policy_surfaces"])
        if overrides:
            receipt.update(overrides)
        self.receipt = receipt
        self.receipt_path = self.trial_dir / "receipt.json"
        self.receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def _clean_trial_arguments(
    scenario: str, trial: int, *, conversation: str
) -> dict[str, Any]:
    """The artifacts of a trial that satisfies every criterion."""
    case = evaluate.SCENARIOS_BY_KEY[scenario].case
    invocations = [
        _invocation(
            check=f"focused:{case}",
            actor="worker",
            started_at="2026-09-12T00:00:00+00:00",
            conversation=conversation,
        ),
        _invocation(
            check=evaluate.CONTRACT_CHECK_NAME,
            actor="runner",
            started_at="2026-09-12T00:00:01+00:00",
        ),
    ]
    if scenario == "focused-retry":
        invocations.insert(
            0,
            _invocation(
                check=f"focused:{case}",
                actor="worker",
                exit_code=1,
                started_at="2026-09-11T23:59:59+00:00",
                conversation=conversation,
            ),
        )
    return {
        "scenario": scenario,
        "trial": trial,
        "invocations": invocations,
        "events": [_gate_event()],
        "conversations": [conversation],
    }


def _accepting_cache(root: Path) -> None:
    for scenario in evaluate.SCENARIOS:
        for trial in range(1, evaluate.REQUIRED_TRIALS + 1):
            SyntheticTrial(
                root,
                **_clean_trial_arguments(
                    scenario.key, trial, conversation=f"conv-{scenario.key}-{trial}"
                ),
            )


class TempRootMixin(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def criteria(self, trial: SyntheticTrial) -> dict[str, Any]:
        evidence = evaluate.load_evidence(trial.receipt)
        results = evaluate.evaluate_trial(
            trial.receipt,
            evidence,
            expected_policy_digest=evaluate.policy_digest(
                evaluate.policy_surfaces(REPO_ROOT)
            ),
            expected_fixture_digest=evaluate.fixture_digest(),
        )
        return {criterion.name: criterion for criterion in results}
