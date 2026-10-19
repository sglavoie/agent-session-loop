"""Review artifact and prompt branches of the fake Codex fixture."""

from __future__ import annotations

import json
from pathlib import Path

from fake_codex_common import (
    STATUS_COMPLETE,
    _arg_value,
    _handoff_status_line,
    _session_files,
)

REVIEW_SESSION_MARKER = "<!-- asl:generated-review-session -->"


def _completed_review_count(plan_dir: Path) -> int:
    """Count generated review sessions already marked complete.

    Repair children carry the distinct ``generated-review-repair-session``
    marker, so they never match the review marker literal.
    """
    count = 0
    for path in _session_files(plan_dir):
        text = path.read_text(encoding="utf-8")
        if REVIEW_SESSION_MARKER not in text:
            continue
        if _handoff_status_line(path) == STATUS_COMPLETE:
            count += 1
    return count


def _findings_review_artifacts(variant: str) -> tuple[str, str]:
    """Return canned ``(final-review.md, final-review.json)`` bodies.

    ``findings`` and ``findings-drift`` describe the same defect — the drift
    variant only shifts evidence line numbers — so their normalized finding
    fingerprints match across review cycles. ``findings-new`` is a different
    defect with a distinct fingerprint.
    """
    import json as _json

    if variant == "findings-new":
        finding_id = "cycle-finding-2"
        title = "Missing error handling in verification executor"
        evidence_path = "agent_session_loop/verification.py:88"
        symbol = "run_check"
        repair_group = "verification-errors"
    elif variant == "findings-drift":
        finding_id = "cycle-finding-1-redetected"
        title = "Unbounded retry loop in runner"
        evidence_path = "agent_session_loop/runner.py:250"
        symbol = "supervise_run"
        repair_group = "runner-retries"
    else:
        finding_id = "cycle-finding-1"
        title = "Unbounded retry loop in runner"
        evidence_path = "agent_session_loop/runner.py:100"
        symbol = "supervise_run"
        repair_group = "runner-retries"

    review_body = (
        "## Findings\n"
        f"- {evidence_path} — {title}.\n"
        "- Checked tests/test_agent_session_loop.py for regression coverage.\n"
        "- Compared docs/agent-session-loop.md for documented behavior.\n"
        "Status: 1 Findings (1 high, 0 med, 0 low)\n"
    )
    artifact = {
        "version": 2,
        "outcome": "findings",
        "findings": [
            {
                "id": finding_id,
                "severity": "high",
                "title": title,
                "evidence": [{"path": evidence_path, "symbol": symbol}],
                "explanation": "Canned finding from the fake harness.",
                "repair_group": repair_group,
                "done_criteria": ["The reviewed defect is resolved."],
                "verification_checks": [
                    {
                        "type": "command",
                        "name": f"{repair_group}-check",
                        "argv": ["python3", "-c", "pass"],
                    }
                ],
            }
        ],
    }
    return review_body, _json.dumps(artifact) + "\n"


REVIEW_VERDICT_RETRY_NOTE = "review verdict validation failed"


def _rejected_verdict_count(body: str) -> int:
    """Count the runner's rejected-verdict Handoff notes in a session body.

    An in-run review-verdict retry reverts the review session to ``pending``
    and appends one such note per rejection, so the count is how many times
    *this* review session has already been re-run. Completed-review counting
    alone cannot see those attempts: the session never stays complete.
    """
    return body.count(REVIEW_VERDICT_RETRY_NOTE)


def _invalid_review_artifacts(variant: str) -> tuple[str, str]:
    """Return artifacts that pass the contract check but fail deep validation.

    The generated review contract only checks that every finding carries a
    truthy ``verification_checks``/``done_criteria``; ``review.load_review_verdict``
    validates their contents. Each variant injects a *different* deep defect so
    its validation error — and therefore its retry fingerprint — differs.
    """
    import json as _json

    finding: dict[str, object] = {
        "id": "invalid-finding-1",
        "severity": "high",
        "title": "Review artifact fails deep validation",
        "evidence": [
            {
                "path": "agent_session_loop/runner.py",
                "symbol": "supervise_run",
            }
        ],
        "explanation": "Canned invalid finding from the fake harness.",
        "repair_group": "invalid-artifact",
        "done_criteria": ["The reviewed defect is resolved."],
        "verification_checks": [
            {
                "type": "command",
                "name": "invalid-check",
                "argv": ["python3", "-c", "pass"],
            }
        ],
    }
    if variant == "invalid-artifact-2":
        # A command check with no argv.
        finding["verification_checks"] = [{"type": "command", "name": "no-argv"}]
    elif variant == "invalid-artifact-3":
        # A structurally present but blank done_criteria entry.
        finding["done_criteria"] = ["   "]
    else:
        # An unsupported check type.
        finding["verification_checks"] = [{"type": "bogus", "name": "not-a-check"}]

    review_body = (
        "## Findings\n"
        "- agent_session_loop/runner.py — review artifact fails deep "
        "validation.\n"
        "- Checked tests/test_agent_session_loop.py for regression coverage.\n"
        "- Compared docs/agent-session-loop.md for documented behavior.\n"
        "Status: 1 Findings (1 high, 0 med, 0 low)\n"
    )
    artifact = {"version": 2, "outcome": "findings", "findings": [finding]}
    return review_body, _json.dumps(artifact) + "\n"


def _write_review_outcome(
    plan_dir: Path,
    outcome: str,
    *,
    write_artifact: bool = True,
    repository_aware: bool = False,
) -> None:
    """Write review artifacts for one scripted review outcome.

    ``write_artifact=False`` models a v1 (markdown-only) review session, where
    a stray ``final-review.json`` would change what the runner validates.
    """
    if outcome == "invalid-markdown":
        # Severity counts that do not add up: passes a v1 contract that only
        # checks the file is non-empty, fails the shared status-line grammar.
        review_body = (
            "## Findings\n"
            "- agent_session_loop/runner.py — miscounted review status line.\n"
            "Status: 2 Findings (1 high, 0 med, 0 low)\n"
        )
        review_json = ""
    elif outcome.startswith("invalid-artifact"):
        review_body, review_json = _invalid_review_artifacts(outcome)
    elif outcome == "all-clear":
        review_body = (
            "## Findings\n"
            "- Reviewed agent_session_loop/runner.py for repair-cycle "
            "behavior.\n"
            "- Checked tests/test_agent_session_loop.py for lifecycle "
            "coverage.\n"
            "- Compared docs/agent-session-loop.md for operator-facing "
            "documentation impact.\n"
            "Status: All clear, no findings\n"
        )
        review_json = '{"version": 2, "outcome": "all-clear", "findings": []}\n'
    else:
        review_body, review_json = _findings_review_artifacts(outcome)
    if repository_aware and review_json:
        artifact = json.loads(review_json)
        artifact["version"] = 3
        for finding in artifact.get("findings", []):
            finding["repo"] = "SVC"
            for evidence in finding.get("evidence", []):
                evidence["path"] = "SVC:" + evidence["path"]
            for check in finding.get("verification_checks", []):
                check["repo"] = "SVC"
        review_json = json.dumps(artifact) + "\n"
    (plan_dir / "final-review.md").write_text(review_body, encoding="utf-8")
    if write_artifact and review_json:
        (plan_dir / "final-review.json").write_text(review_json, encoding="utf-8")


def _blocked_last_message() -> str:
    """Return a realistic multi-question blocked-pause last-message body."""
    return (
        "I need decisions on a few open questions before I can continue:\n"
        "\n"
        "1. Should the new endpoint use (a) cursor-based pagination or "
        "(b) offset-based pagination?\n"
        "2. For request authentication, do we (a) reuse the existing "
        "middleware or (b) introduce a per-route guard?\n"
        "3. For audit logging, (a) log every request synchronously or "
        "(b) batch writes every 30 seconds?\n"
        "\n"
        "Reply with one selection per question (e.g., `1a, 2a, 3b`).\n"
        "\n"
        "Runner status: paused\n"
        "RUNNER_PAUSE: blocked - interactive decision gates require user answers\n"
    )


def _write_last_message(text: str) -> None:
    last_message = _arg_value("-o")
    if last_message is None:
        return
    path = Path(last_message)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
