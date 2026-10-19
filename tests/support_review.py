from .support_base import *
from .support_plans import write_plan, write_plan_with_ids


def _baseline_contract_text(json_block: str) -> str:
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- something",
            "",
            "### Verify",
            "",
            "```json",
            json_block,
            "```",
            "",
            "## Handoff",
            "- **Status**: pending",
        ]
    )


def _command_contract(
    argv: str,
    *,
    session: str = "1",
    name: str = "cmd",
    expectation: str = "pass",
) -> str:
    return _contract_block(
        (
            f'{{"type": "command", "argv": {argv}, "name": "{name}", '
            f'"baseline_expectation": "{expectation}"}}'
        ),
        session=session,
    )


class FakeRunControlListener:
    def __init__(
        self,
        *,
        stop_requested: bool = False,
        actions_on_start: tuple[run_control.RunControlAction, ...] = (),
        status_on_start: tuple[run_control.RunControlAction, ...] = (),
        state_path: Path | None = None,
        events_path: Path | None = None,
        current_harness: str | None = None,
    ) -> None:
        self.actions_on_start = actions_on_start
        self.status_on_start = status_on_start
        self.state_path = state_path
        self.events_path = events_path
        self.on_action: run_control.ActionCallback | None = None
        self.on_status: run_control.StatusCallback | None = None
        self.start_calls = 0
        self.stop_calls = 0
        self.consume_calls = 0
        self.abort_consume_calls = 0
        self.state_exists_at_start = False
        self.events_exists_at_start = False
        self.state = run_control.RunControlState(current_harness=current_harness)
        if stop_requested:
            self.state.request_stop_after_current()

    def start(self) -> bool:
        self.start_calls += 1
        self.state_exists_at_start = (
            self.state_path.exists() if self.state_path is not None else False
        )
        self.events_exists_at_start = (
            self.events_path.exists() if self.events_path is not None else False
        )
        if self.on_action is not None:
            for action in self.actions_on_start:
                self.on_action(action)
        if self.on_status is not None:
            for action in self.status_on_start:
                self.on_status(action)
        return True

    def stop(self) -> None:
        self.stop_calls += 1

    def consume_stop_after_current(self) -> run_control.RunControlAction | None:
        # The runner peeks the shared state at every checkpoint and consumes
        # only when it honors the request, so the fake has to route both
        # through the real state rather than faking the consume side.
        self.consume_calls += 1
        return self.state.consume_stop_after_current()

    def consume_abort_turn(self) -> run_control.RunControlAction | None:
        self.abort_consume_calls += 1
        return self.state.consume_abort_turn()


def _contract_block(
    check_payload: str, *, session: str = "1", parallel: bool = False
) -> str:
    parallel_field = ', "parallel": true' if parallel else ""
    return "\n".join(
        [
            "## Completion Contract",
            "",
            "### Done when",
            "- recipe runs",
            "",
            "### Verify",
            "",
            "```json",
            f'{{"session": "{session}"{parallel_field}, "checks": [{check_payload}]}}',
            "```",
        ]
    )


def _write_loop_ready_plan(
    plan_dir: Path,
    statuses: tuple[str, ...],
    *,
    contracts_by_id: dict[str, str] | None = None,
    state: plans_models.ReadinessState = "loop-ready",
) -> plans_models.ReadinessMetadata:
    """Write a plan with valid contracts and a synced readiness block."""
    contract_blocks: dict[str, str] = {}
    for number in range(1, len(statuses) + 1):
        key = str(number)
        contract_blocks[key] = (contracts_by_id or {}).get(
            key,
            _contract_block(
                '{"type": "file_exists", "path": "README.md", '
                f'"name": "readme-{number}"}}',
                session=key,
            ),
        )
    write_plan(plan_dir, statuses, contracts_by_id=contract_blocks)
    expected = plans_content_hash.stable_readiness_sha256(plan_dir)
    metadata = plans_models.ReadinessMetadata(
        state=state,
        readiness_sha256=expected,
        checked_at="2026-06-19T00:00:00Z",
    )
    plans_readiness.write_readiness_metadata(plan_dir, metadata)
    return metadata


_FIX_CATEGORIES = {
    "plan-recreate",
    "filename-rename",
    "manifest-update",
    "plan-edit",
    "invocation-fix",
    "env-install",
    "env-state",
    "session-recipe-edit",
    "informational",
}


def _structured_review_finding(
    *,
    finding_id: str = "F1",
    severity: str = "high",
    **overrides: object,
) -> dict[str, Any]:
    """Return a schema-complete structured review finding for tests."""
    finding: dict[str, Any] = {
        "id": finding_id,
        "severity": severity,
        "title": f"Finding {finding_id} title",
        "evidence": [{"path": "agent_session_loop/runner.py", "symbol": "run_loop"}],
        "explanation": f"Explanation for {finding_id}.",
        "repair_group": "group-1",
        "done_criteria": ["The issue is fixed and covered by a test."],
        "verification_checks": [
            {
                "type": "command",
                "name": f"verify-{finding_id}",
                "argv": ["python3", "-c", "pass"],
            }
        ],
    }
    finding.update(overrides)
    return finding


def write_completed_review_plan(
    plan_dir: Path,
    findings: list[dict[str, Any]],
    *,
    contracts_by_id: dict[str, str] | None = None,
) -> Path:
    """Write a two-session plan ending in one completed generated review."""
    write_plan_with_ids(
        plan_dir,
        (("1", "complete"), ("2", "complete")),
        contracts_by_id=contracts_by_id,
    )
    review_body = review_session_body.build_generated_review_session_body(
        plans_session_ids.parse_session_id("2"),
        review_harness="codex",
        plan_dir=plan_dir,
    ).replace("- **Status**: pending", "- **Status**: complete")
    (plan_dir / "session-2.md").write_text(review_body, encoding="utf-8")
    if findings:
        high = sum(1 for finding in findings if finding["severity"] == "high")
        med = sum(1 for finding in findings if finding["severity"] == "med")
        low = sum(1 for finding in findings if finding["severity"] == "low")
        status_line = (
            f"Status: {len(findings)} Findings ({high} high, {med} med, {low} low)"
        )
        outcome = "findings"
    else:
        status_line = review.REVIEW_STATUS_ALL_CLEAR
        outcome = "all-clear"
    (plan_dir / review.FINAL_REVIEW_FILENAME).write_text(
        f"# Final review\n\n{status_line}\n", encoding="utf-8"
    )
    (plan_dir / review.FINAL_REVIEW_ARTIFACT_FILENAME).write_text(
        json.dumps({"version": 2, "outcome": outcome, "findings": findings}),
        encoding="utf-8",
    )
    return plans_files.canonicalize_path(plan_dir)


def write_review_cycle_event(
    plan_dir: Path,
    state_root: Path,
    payload: dict[str, Any],
) -> Path:
    """Append one review-cycle fixture event and return its event path."""
    event_path = state.plan_state_dir(plan_dir, state_root) / events.EVENTS_FILE_NAME
    events.append_event(event_path, events.Event(events.REVIEW_CYCLE_EVENT, payload))
    return event_path
