"""One-shot execution branch of the fake Codex fixture."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from fake_codex_acp import (
    _signal_stream_ready,
    _step_sleep,
    _wait_for_stream_continue,
    run_acp,
)
from fake_codex_common import (
    _append_future_note,
    _apply_workspace_mutation,
    _apply_workspace_mutation_record,
    _arg_value,
    _mark_complete,
    _pending_sessions,
    _prompt_contains_reply_context,
    _prompt_contains_resolution_context,
    _session_label,
    _session_sort_key,
    _tamper_with_named_session,
)
from fake_codex_review import (
    REVIEW_SESSION_MARKER,
    _blocked_last_message,
    _completed_review_count,
    _rejected_verdict_count,
    _write_last_message,
    _write_review_outcome,
)
from fake_codex_turn_setup import (
    _commit_prompt,
    _run_commit_message,
    _run_prepare_validator,
    _validation_plan_dir,
    _validation_prompt,
)


def main() -> int:
    if sys.argv[1:2] == ["app-server"]:
        # This fixture speaks the one-shot `codex exec` command line, not the
        # app-server JSON-RPC protocol. Declining immediately -- before reading
        # a byte of stdin, so before `turn/start` and before any work -- is
        # exactly the state the adapter's pre-start fallback exists for, so
        # every suite built on this fixture keeps exercising the legacy
        # transport end to end. The app-server transport has its own fake in
        # tests/test_asl_codex_app_server.py.
        print("fake codex does not implement app-server", file=sys.stderr)
        return 2

    if "--acp" in sys.argv:
        return run_acp(_run_turn)
    return _run_turn()


def _run_turn() -> int:
    validation_prompt = _validation_prompt()
    if validation_prompt is not None:
        validation_plan_dir = _validation_plan_dir(validation_prompt)
        if validation_plan_dir is None:
            print("missing shared context path in validation packet", file=sys.stderr)
            return 2
        return _run_prepare_validator(validation_plan_dir, validation_prompt)

    commit_prompt = _commit_prompt()
    if commit_prompt is not None:
        return _run_commit_message(commit_prompt)

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

    spark_failure = os.environ.get("FAKE_CODEX_SPARK_FAILURE")
    if _arg_value("--model") == "gpt-5.3-codex-spark" and spark_failure:
        if spark_failure == "quota":
            message = "usage limit reached; retry-after: 3600"
        elif spark_failure == "invalid-model":
            message = "model gpt-5.3-codex-spark is not available"
        else:
            message = spark_failure
        print(message, file=sys.stderr)
        _write_last_message(message + "\n")
        return 7

    _apply_workspace_mutation()
    _tamper_with_named_session(plan_dir)
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
        if _prompt_contains_resolution_context():
            if pending:
                _mark_complete(pending[0])
            _write_last_message(
                "Runner status: complete\nNext command: $session-run plan/\n"
            )
            print("completed one session after consuming supervisor resolution")
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
        _write_last_message(
            "Runner status: complete\nNext command: $session-run plan/\n"
        )
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
            if "asl:generated-review-repair-session" in body:
                repair_mutation = os.environ.get("FAKE_CODEX_REPAIR_MUTATION_PATH")
                if repair_mutation:
                    _apply_workspace_mutation_record(
                        {
                            "path": repair_mutation,
                            "action": "write",
                            "text": os.environ.get(
                                "FAKE_CODEX_REPAIR_MUTATION_TEXT", "repaired\n"
                            ),
                        }
                    )
                if os.environ.get("FAKE_CODEX_REPAIR_COMMIT"):
                    subprocess.run(["git", "add", "-A"], check=True)
                    subprocess.run(["git", "commit", "-m", "worker repair"], check=True)
            if REVIEW_SESSION_MARKER in body:
                review_mutation = os.environ.get("FAKE_CODEX_REVIEW_MUTATION_PATH")
                if review_mutation:
                    _apply_workspace_mutation_record(
                        {
                            "path": review_mutation,
                            "action": os.environ.get(
                                "FAKE_CODEX_REVIEW_MUTATION_ACTION", "write"
                            ),
                            "text": os.environ.get(
                                "FAKE_CODEX_REVIEW_MUTATION_TEXT", "mutated\n"
                            ),
                            "destination": os.environ.get(
                                "FAKE_CODEX_REVIEW_MUTATION_DESTINATION"
                            ),
                        }
                    )
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
                    attempt_index = review_index + _rejected_verdict_count(body)
                    outcome = outcomes[min(attempt_index, len(outcomes) - 1)]
                    _write_review_outcome(
                        plan_dir,
                        outcome,
                        write_artifact=(
                            "asl:generated-review-session version=" in body
                        ),
                        repository_aware=(
                            "asl:generated-review-session version=5" in body
                        ),
                    )
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
                            (
                                '{"version": 3, "outcome": "all-clear", '
                                '"findings": []}\n'
                                if "asl:generated-review-session version=5" in body
                                else '{"version": 2, "outcome": "all-clear", '
                                '"findings": []}\n'
                            ),
                        )
                        (plan_dir / "final-review.json").write_text(
                            review_json, encoding="utf-8"
                        )
                if os.environ.get("FAKE_CODEX_REVIEW_BREAK_PLAN"):
                    # The review artifacts stay on disk while the plan itself
                    # becomes uninspectable, so the turn ends at the
                    # malformed-plan branch instead of at the commit that
                    # would otherwise be the only record of the verdict.
                    (plan_dir / "context.md").unlink(missing_ok=True)
        _write_last_message(
            "Runner status: complete\nNext command: $session-run plan/\n"
        )
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
        _write_last_message(
            "Runner status: complete\nNext command: $session-run plan/\n"
        )
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
        _write_last_message(
            "Runner status: complete\nNext command: $session-run plan/\n"
        )
        print(f"session {session_label} stdout after completion", flush=True)
        print(
            f"session {session_label} stderr after completion",
            file=sys.stderr,
            flush=True,
        )
        return 0

    print(f"unsupported fake codex mode: {mode}", file=sys.stderr)
    return 2
