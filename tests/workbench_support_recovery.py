"""Isolated, on-disk recovery scenarios for both Workbench deployments."""

from __future__ import annotations

import json
import time
from pathlib import Path

from agent_session_loop import (
    events,
    redirect_store_models,
    redirect_store_requests,
    state,
    workbench_catalog,
)

from .support_workbench import _WORKBENCH_DEAD_PID, _write_lock_file
from .workbench_support import (
    SHELL_PLAN_NAMES,
    SHELL_RUN_IDS,
    ShellDeployments,
    seed_shell_plans,
    shell_iso,
    start_shell_deployments,
)

RETAINED_OPERATION_ID = "browser-retained-correction"


def start_recovery_deployments(root: Path, scenario: str) -> ShellDeployments:
    """Serve a fresh corpus per test so an actual unlock cannot leak to another.

    Only alpha changes. The remote catalog is projected from those same files,
    and its detail lane reads them through the real ASL responder. Mutations
    keep the normal fixture semantics: the Mac executes an unlock, while the Pi
    records a request that has not yet been carried to the Mac.
    """
    corpus = seed_shell_plans(root)
    plan_dir = corpus.plan_dir("alpha")
    plan_state = state.plan_state_dir(plan_dir, corpus.state_dir)
    run_id = SHELL_RUN_IDS["alpha"]
    payload = {
        "plan_dir": str(plan_dir),
        "run_id": run_id,
        "session_id": "2",
        "ok": False,
        "paused": True,
        "updated_at": time.time(),
    }
    if scenario == "stale-lock":
        payload.update(
            ok=True,
            paused=False,
            reason="stopped",
            run_finished_at=shell_iso(time.time()),
        )
        _write_lock_file(plan_dir, corpus.state_dir, pid=_WORKBENCH_DEAD_PID)
    elif scenario == "verify-failed":
        artifact_dir = plan_state / "runs" / str(run_id) / "turn-0001"
        artifact_dir.mkdir(parents=True)
        result_path = artifact_dir / "result.json"
        result_path.write_text('{"ok": false}\n', encoding="utf-8")
        verification = {
            "session": "2",
            "reason": "verify-failed",
            "passed": False,
            "checks": [
                {
                    "name": "expected-report",
                    "status": "fail",
                    "failure_reason": "file-missing",
                }
            ],
        }
        payload.update(
            reason="verify-failed",
            pause_reason="verify-failed",
            pause_message="verification failed for session 2",
            pause_details={"failures": verification["checks"]},
            turns=[
                {
                    "session_id": "2",
                    "turn_index": 1,
                    "result_path": str(result_path),
                    "verification": verification,
                }
            ],
        )
    elif scenario in ("fresh-conversation", "blocked-predecessor"):
        reason = (
            events.PAUSE_REASON_CONVERSATION_RECOVERY_REQUIRED
            if scenario == "fresh-conversation"
            else events.PAUSE_REASON_REDIRECT_SUCCESSOR_BLOCKED
        )
        payload.update(reason=reason, pause_reason=reason)
        assert run_id is not None
        redirect_store_requests.submit_request(
            plan_dir,
            target=redirect_store_models.RedirectTarget(
                run_id=run_id, session_id="2", turn_index=1, harness="codex"
            ),
            feedback="Keep the operator's correction when recovering the conversation.",
            state_dir=corpus.state_dir,
            operation_id=RETAINED_OPERATION_ID,
        )
    else:
        raise ValueError(f"Unknown recovery scenario: {scenario}")
    (plan_state / events.STATE_FILE_NAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    scan = workbench_catalog.scan(
        state_dir=corpus.state_dir,
        plan_roots=corpus.plan_roots,
        include_default_dirs=False,
    )
    assert scan.complete, scan.projection_warnings
    entries = {entry.plan_key: entry.to_payload() for entry in scan.catalog.entries()}
    return start_shell_deployments(
        root,
        corpus=corpus,
        catalog_entries=[entries[corpus.plan_keys[name]] for name in SHELL_PLAN_NAMES],
    )
