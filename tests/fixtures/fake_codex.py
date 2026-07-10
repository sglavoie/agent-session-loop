#!/usr/bin/env python3
"""Fake Codex executable used by agent-session-loop integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import time


STATUS_PENDING = "- **Status**: pending"
STATUS_COMPLETE = "- **Status**: complete"
HANDOFF_HEADING = "## Handoff"
SESSION_FILE_RE = re.compile(r"^session-([1-9][0-9]*(?:\.[1-9][0-9]*)*)\.md$")


def _arg_value(flag: str) -> str | None:
    try:
        index = sys.argv.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(sys.argv):
        return None
    return sys.argv[index + 1]


def _session_id(path: Path) -> str | None:
    match = SESSION_FILE_RE.fullmatch(path.name)
    if match is None:
        return None
    return match.group(1)


def _session_sort_key(path: Path) -> tuple[int, ...]:
    session_id = _session_id(path)
    if session_id is None:
        raise ValueError(f"not a session file: {path.name}")
    return tuple(int(component) for component in session_id.split("."))


def _session_files(plan_dir: Path) -> list[Path]:
    return sorted(
        (path for path in plan_dir.glob("session-*.md") if _session_id(path)),
        key=_session_sort_key,
    )


def _pending_sessions(plan_dir: Path) -> list[Path]:
    return [
        path
        for path in _session_files(plan_dir)
        if _handoff_status_line(path) == STATUS_PENDING
    ]


def _prompt_contains_reply_context() -> bool:
    for value in sys.argv[1:]:
        if "## ASL reply context" in value:
            return True
    return False


def _session_label(path: Path) -> str:
    session_id = _session_id(path)
    if session_id is None:
        raise ValueError(f"not a session file: {path.name}")
    return session_id


def _handoff_status_line(path: Path) -> str | None:
    lines = path.read_text(encoding="utf-8").splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        for line in lines[index + 1 :]:
            if line.startswith("## "):
                return None
            if line.startswith("- **Status**:"):
                return line
        return None
    return None


def _mark_complete(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        for status_index in range(index + 1, len(lines)):
            if lines[status_index].startswith("## "):
                break
            if lines[status_index] == STATUS_PENDING:
                lines[status_index] = STATUS_COMPLETE
                suffix = "\n" if text.endswith("\n") else ""
                path.write_text("\n".join(lines) + suffix, encoding="utf-8")
                return
        return


def _append_future_note(path: Path, *, source_session_id: str) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() != HANDOFF_HEADING:
            continue
        note = [
            f"## Notes from session-{source_session_id}",
            "",
            "- Generated future-session note from fake harness.",
            "",
        ]
        suffix = "\n" if text.endswith("\n") else ""
        path.write_text(
            "\n".join(lines[:index] + note + lines[index:]) + suffix,
            encoding="utf-8",
        )
        return


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


def _write_review_outcome(plan_dir: Path, outcome: str) -> None:
    """Write review artifacts for one scripted review outcome."""
    if outcome == "all-clear":
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
    (plan_dir / "final-review.md").write_text(review_body, encoding="utf-8")
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


def _step_sleep() -> None:
    time.sleep(float(os.environ.get("FAKE_CODEX_STEP_SLEEP", "0.1")))


def _stream_gate_dir() -> Path | None:
    value = os.environ.get("FAKE_CODEX_STREAM_GATE_DIR")
    if not value:
        return None
    return Path(value)


def _signal_stream_ready(session_label: str) -> None:
    gate_dir = _stream_gate_dir()
    if gate_dir is None:
        return
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / f"session-{session_label}.ready").write_text(
        "ready\n",
        encoding="utf-8",
    )


def _wait_for_stream_continue(session_label: str) -> bool:
    gate_dir = _stream_gate_dir()
    if gate_dir is None:
        return True

    continue_path = gate_dir / f"session-{session_label}.continue"
    timeout = float(os.environ.get("FAKE_CODEX_STREAM_GATE_TIMEOUT", "30"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if continue_path.exists():
            return True
        time.sleep(0.02)
    print(
        f"timed out waiting for stream gate: {continue_path}",
        file=sys.stderr,
        flush=True,
    )
    return False


def main() -> int:
    plan_dir_arg = _arg_value("--add-dir")
    if plan_dir_arg is None:
        print("missing --add-dir", file=sys.stderr)
        return 2

    mode = os.environ.get("FAKE_CODEX_MODE", "success")
    plan_dir = Path(plan_dir_arg)

    _write_last_message(f"fake codex mode: {mode}\n")
    stdout_text = os.environ.get("FAKE_CODEX_STDOUT_TEXT")
    if stdout_text is not None:
        print(stdout_text, end="", flush=True)
    stderr_text = os.environ.get("FAKE_CODEX_STDERR_TEXT")
    if stderr_text is not None:
        print(stderr_text, end="", file=sys.stderr, flush=True)

    if mode == "timeout":
        time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "5")))
        return 0

    if mode == "failure":
        print("fake codex failure", file=sys.stderr)
        _write_last_message("fake codex failure\n")
        return 7

    pending = _pending_sessions(plan_dir)
    if mode == "no-progress":
        _write_last_message("Runner status: complete\n")
        print("left plan unchanged")
        return 0

    if mode == "multiple-progress":
        for path in pending[:2]:
            _mark_complete(path)
        _write_last_message("Runner status: complete\n")
        print("completed multiple sessions")
        return 0

    if mode == "unexpected-progress":
        if len(pending) > 1:
            _mark_complete(pending[1])
        _write_last_message("Runner status: complete\n")
        print("completed unexpected session")
        return 0

    if mode == "runner-pause":
        _write_last_message("RUNNER_PAUSE: operator requested pause\n")
        print("runner requested pause")
        return 0

    if mode == "runner-pause-unsafe":
        _write_last_message(
            "RUNNER_PAUSE: unsafe - plan readiness is not current; "
            "run `asl preflight --mark-loop-ready` before continuing\n"
        )
        print("runner requested unsafe pause")
        return 0

    if mode == "reply-blocked":
        _write_last_message(_blocked_last_message())
        print("runner pause: blocked on operator answers")
        return 0

    if mode == "reply-aware":
        if _prompt_contains_reply_context():
            if pending:
                _mark_complete(pending[0])
            _write_last_message(
                "Runner status: complete\nNext command: $session-run plan/\n"
            )
            print("completed one session after consuming operator reply")
            return 0
        _write_last_message(_blocked_last_message())
        print("runner pause: blocked on operator answers")
        return 0

    if mode == "malformed-after-turn":
        (plan_dir / "context.md").unlink(missing_ok=True)
        _write_last_message("Runner status: complete\n")
        print("mutated plan after turn")
        return 0

    if mode == "success-runner-pause-none":
        if pending:
            _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nRUNNER_PAUSE: none\n")
        print("completed one session with a no-op pause marker")
        return 0

    if mode == "success":
        if pending:
            _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session")
        return 0

    if mode == "success-with-review":
        target = pending[0] if pending else None
        if target is not None:
            # Index scripted review outcomes by how many generated review
            # sessions were already complete before this turn.
            review_index = _completed_review_count(plan_dir)
            _mark_complete(target)
            # When the harness is asked to run a generated review session,
            # the file body carries this marker and the session must produce
            # ``final-review.md`` inside the plan directory. Sessions
            # generated at version >= 2 additionally require the structured
            # ``final-review.json`` artifact.
            body = target.read_text(encoding="utf-8")
            if REVIEW_SESSION_MARKER in body:
                outcomes_env = os.environ.get("FAKE_CODEX_REVIEW_OUTCOMES")
                if outcomes_env:
                    # Scripted per-review outcomes for repair/rereview cycle
                    # tests: e.g. "findings,all-clear". The list is clamped
                    # to its last entry for any additional review.
                    outcomes = [
                        token.strip()
                        for token in outcomes_env.split(",")
                        if token.strip()
                    ]
                    outcome = outcomes[min(review_index, len(outcomes) - 1)]
                    _write_review_outcome(plan_dir, outcome)
                else:
                    review_body = os.environ.get(
                        "FAKE_CODEX_REVIEW_BODY",
                        (
                            "## Findings\n"
                            "- Reviewed agent_session_loop/runner.py for generated "
                            "review contract behavior.\n"
                            "- Checked tests/test_agent_session_loop.py for lifecycle "
                            "coverage.\n"
                            "- Compared docs/agent-session-loop.md for operator-facing "
                            "documentation impact.\n"
                            "Status: All clear, no findings\n"
                        ),
                    )
                    (plan_dir / "final-review.md").write_text(
                        review_body, encoding="utf-8"
                    )
                    if "asl:generated-review-session version=" in body:
                        review_json = os.environ.get(
                            "FAKE_CODEX_REVIEW_JSON",
                            '{"version": 2, "outcome": "all-clear", "findings": []}\n',
                        )
                        (plan_dir / "final-review.json").write_text(
                            review_json, encoding="utf-8"
                        )
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session (review-aware)")
        return 0

    if mode == "success-future-note":
        target = pending[0] if pending else None
        if target is not None:
            source_session_id = _session_label(target)
            _mark_complete(target)
            remaining = [
                path
                for path in _pending_sessions(plan_dir)
                if _session_sort_key(path) > _session_sort_key(target)
            ]
            if remaining:
                _append_future_note(
                    remaining[0],
                    source_session_id=source_session_id,
                )
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print("completed one session and added a future note")
        return 0

    if mode == "streaming-success":
        if not pending:
            _write_last_message("Runner status: complete\n")
            print("no pending sessions", flush=True)
            return 0

        session_label = _session_label(pending[0])
        print(f"session {session_label} stdout before completion", flush=True)
        print(
            f"session {session_label} stderr before completion",
            file=sys.stderr,
            flush=True,
        )
        _signal_stream_ready(session_label)
        if not _wait_for_stream_continue(session_label):
            _write_last_message("fake codex stream gate timeout\n")
            return 3
        _step_sleep()
        _mark_complete(pending[0])
        _write_last_message("Runner status: complete\nNext command: $session-run plan/\n")
        print(f"session {session_label} stdout after completion", flush=True)
        print(
            f"session {session_label} stderr after completion",
            file=sys.stderr,
            flush=True,
        )
        return 0

    print(f"unsupported fake codex mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
