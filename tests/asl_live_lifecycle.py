"""Opt-in acceptance of a prepared plan through the real operator commands.

Invoked only by ``asl_live_redirect_acceptance --suite lifecycle``. Ordinary
test discovery never launches this suite or spends provider quota.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from agent_session_loop import harness_compatibility, state
from agent_session_loop.harnesses import HarnessName
from tests.asl_live_lifecycle_process import (
    HELPER,
    MAX_HARNESS_LAUNCHES,
    Commands,
    source_identity,
)
from tests.asl_live_redirect_workspace import Workspace, disposable_workspace

CHECKS = (
    "prepare-and-preflight",
    "interrupt-and-correct",
    "stop-and-resume",
    "edits-preserved",
    "correction-delivered-once",
    "verified-completion",
)


def write_project(workspace: Workspace, nonce: str) -> Path:
    """A small, independently checkable project whose first tool must be stopped."""
    checker = """import pathlib, sys
p = pathlib.Path
nonce = sys.argv[2]
assert p("seed.txt").read_text() == nonce + "\\n", "seed changed"
assert p("corrections.txt").read_text().splitlines() == [nonce], "correction count"
assert p("stage1.txt").read_text() == nonce + "\\n", "stage 1 missing"
if sys.argv[1] == "2":
    assert p("result.txt").read_text() == "complete:" + nonce + "\\n", "result missing"
"""
    (workspace.repo / "verify_lifecycle.py").write_text(checker)
    # The verifier belongs to the initial repository, so workers cannot satisfy
    # a contract by replacing its implementation without the final byte check.
    import subprocess

    subprocess.run(
        ["git", "add", "verify_lifecycle.py"], cwd=workspace.repo, check=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "Add acceptance verifier"],
        cwd=workspace.repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/asl-live.git"],
        cwd=workspace.repo,
        check=True,
        capture_output=True,
    )
    source = workspace.root / "lifecycle.md"
    lines = [
        "# ASL live lifecycle acceptance",
        "",
        "This disposable project deliberately waits for a supervisor correction.",
        "That intervention is part of the test, not an unanswered design question.",
        "Do not commit. Follow session-run's handoff protocol; the runner verifies.",
        "Never edit verify_lifecycle.py. All created files are in the repository.",
        f"The acceptance nonce is {nonce}.",
        "Every file path below is relative to the APP repository root (repo/).",
        "Run shell tools from that root, not the parent workspace.",
        "",
        "## Repositories",
        "",
        "| Alias | Path | Origin |",
        "| --- | --- | --- |",
        "| APP | repo | https://example.invalid/asl-live.git |",
        "",
        "## Session 1: Write a seed and receive the correction",
        "",
        "**Reads repositories**: APP",
        "**Writes repositories**: APP",
        "",
        "Before a correction arrives, create seed.txt containing the nonce "
        "and a newline.",
        "Then run one shell tool that writes running.txt and sleeps for 600 seconds:",
        '`python3 -c "from pathlib import Path; import time; '
        "Path('running.txt').write_text('running\\n'); time.sleep(600)\"`.",
        "The supervisor will interrupt this tool. Do not mark the session complete",
        "until you have received and followed the urgent correction. On continuation,",
        "do not restart the sleep or rewrite seed.txt. The correction tells you how",
        "to create corrections.txt and stage1.txt and complete this session.",
        "",
    ]
    for number in (1, 2):
        if number == 2:
            lines.extend(
                [
                    "## Session 2: Finish the preserved project",
                    "",
                    "**Reads repositories**: APP",
                    "**Writes repositories**: APP",
                    "",
                    "Read seed.txt, stage1.txt, and corrections.txt. "
                    "Preserve them byte for byte.",
                    "Write result.txt containing complete: followed by the nonce "
                    "and a newline.",
                    "Do not repeat the prior session's correction or append a receipt.",
                    "Complete this session's handoff. Do not modify any other session.",
                    "",
                ]
            )
        lines.extend(
            [
                "## Completion Contract",
                "",
                "### Done when",
                "",
                f"- Stage {number} files contain the expected nonce; "
                "the seed is preserved",
                "  and exactly one correction receipt exists.",
                "",
                "### Verify",
                "",
                "```json",
                json.dumps(
                    {
                        "session": str(number),
                        "checks": [
                            {
                                "name": f"stage-{number}",
                                "type": "command",
                                "repo": "APP",
                                "argv": [
                                    sys.executable,
                                    "verify_lifecycle.py",
                                    str(number),
                                    nonce,
                                ],
                                "timeout_seconds": 15,
                            }
                        ],
                    }
                ),
                "```",
                "",
            ]
        )
    source.write_text("\n".join(lines))
    return source


def supervision(
    commands: Commands,
    plan: Path,
    handshake: dict,
    *,
    marker: Path | None = None,
) -> dict | None:
    """Poll the launch-scoped outbox until a marker or terminal verdict exists."""
    while True:
        payload = commands.asl(
            "supervise",
            "outbox",
            "supervise",
            str(plan),
            "--state-dir",
            str(commands.root / "state"),
            "--supervision-id",
            handshake["supervision_id"],
            "--timeout",
            "1",
        )
        token = payload["state"]
        if token == "DONE":
            result = json.loads(Path(handshake["result_path"]).read_text())
            if marker is not None:
                raise RuntimeError(
                    f"run ended before the interruption marker: {result}"
                )
            return result
        if token != "IDLE":
            raise RuntimeError(f"unexpected supervision outcome: {payload}")
        if marker is not None and marker.is_file():
            return None


def launch(commands: Commands, argv: list[str], label: str) -> dict:
    handshake = commands.call(
        label,
        [
            sys.executable,
            str(HELPER),
            "--command",
            shlex.join(argv),
            "--supervision-id",
            str(uuid.uuid4()),
            "--json",
            "--detach",
            "--supervision-log-dir",
            str(commands.root / label),
        ],
    )
    commands.remember_helper(handshake)
    return handshake


def run_sequence(
    workspace: Workspace, commands: Commands, harness: HarnessName
) -> dict:
    nonce = uuid.uuid4().hex
    source = write_project(workspace, nonce)
    verifier = (workspace.repo / "verify_lifecycle.py").read_bytes()
    prepared = commands.asl(
        "prepare",
        "prepare",
        str(source),
        "--cwd",
        str(workspace.root),
        "--harness",
        harness,
        "--validator-harness",
        harness,
        "--validator-jobs",
        "1",
        "--validator-timeout",
        "240",
        "--base-dir",
        str(workspace.root / "plans"),
        "--state-dir",
        str(workspace.state_dir),
        timeout=commands.remaining(),
    )
    if not prepared.get("ok") or not prepared.get("marked_loop_ready"):
        raise RuntimeError(f"prepare did not mark the plan loop-ready: {prepared}")
    plan = Path(prepared["plan_dir"])
    common = [str(plan), "--state-dir", str(workspace.state_dir)]
    preflight = commands.asl(
        "preflight", "preflight", *common, "--cwd", str(workspace.root)
    )
    if not preflight.get("ok"):
        raise RuntimeError(f"preflight failed: {preflight}")
    # Use prepare's command, adding only explicit execution budgets. No audit,
    # commit generator, review, or automatic repair introduces extra paid turns.
    run = shlex.split(prepared["run_command"]) + [
        "--state-dir",
        str(workspace.state_dir),
        "--timeout",
        "240",
        "--no-supervisor-audit",
        "--max-repair-group-attempts",
        "1",
        "--max-review-fix-cycles",
        "0",
    ]
    first = launch(commands, [*run, "--max-sessions", "1"], "first-supervision")
    supervision(commands, plan, first, marker=workspace.repo / "running.txt")
    before = commands.asl("before-interruption", "status", *common)
    seed = (workspace.repo / "seed.txt").read_bytes()
    if seed != (nonce + "\n").encode():
        raise RuntimeError(
            "the worker did not write the expected seed before interruption"
        )
    correction = (
        "This is the planned acceptance correction. Stop the sleeping tool and do not "
        "restart it. Preserve seed.txt. Append exactly one line containing "
        f"{nonce} to corrections.txt (do not deduplicate or erase existing lines). "
        f"Write stage1.txt containing {nonce} and a newline. "
        "Acknowledge the correction using the supplied ASL acknowledgement command. "
        "Complete only session 1 and write its complete handoff."
    )
    redirected = commands.asl(
        "redirect",
        "control",
        "redirect",
        *common,
        "--session",
        "1",
        "--text",
        correction,
        "--ttl",
        "900",
        "--timeout",
        "30",
    )
    # A timed-out acceptance is uncertain. Never submit a second operation.
    if not redirected.get("ok"):
        raise RuntimeError(f"correction was not accepted: {redirected}")
    stopped = supervision(commands, plan, first)
    if not stopped or stopped.get("reason") != "max-sessions":
        raise RuntimeError(
            f"the first supervision did not stop at its session budget: {stopped}"
        )
    midpoint = commands.asl("stopped-status", "status", *common)
    state_path = state.plan_state_dir(plan, workspace.state_dir) / "state.json"
    first_state = json.loads(state_path.read_text())
    if (
        not first_state.get("run_finished_at")
        or first_state.get("active_turn")
        or first_state.get("active_verification")
        or midpoint["counts"]["complete"] != 1
        or midpoint["counts"]["pending"] != 1
        or midpoint["lock"]["active"]
    ):
        raise RuntimeError(
            "the first run did not stop with one verified session pending"
        )
    first_turns = first_state.get("turns", [])
    if (
        len(first_turns) != 2
        or first_turns[0].get("redirect_continuation", {}).get("outcome") != "continued"
        or not first_turns[1].get("verification", {}).get("passed")
    ):
        raise RuntimeError(
            "the first run lacks an interrupted turn and verified successor"
        )
    receipts_before = (workspace.repo / "corrections.txt").read_bytes()
    second = launch(
        commands, [*run, "--resume", "--max-sessions", "2"], "resumed-supervision"
    )
    finished = supervision(commands, plan, second)
    if not finished or not finished.get("ok") or finished.get("reason") != "complete":
        raise RuntimeError(f"resumed supervision did not complete: {finished}")
    final = commands.asl("final-status", "status", *common)
    journal = commands.asl("redirect-status", "control", "redirect-status", *common)
    operations = journal["operations"]
    if len(operations) != 1 or operations[0]["stage"] != "acknowledged":
        raise RuntimeError(f"correction lacks one acknowledged operation: {operations}")
    records = [
        json.loads(line)
        for line in Path(journal["journal_path"]).read_text().splitlines()
    ]
    deliveries = sum(row.get("kind") == "delivered" for row in records)
    if deliveries != 1:
        raise RuntimeError(
            f"expected exactly one durable correction delivery, got {deliveries}"
        )
    if (workspace.repo / "seed.txt").read_bytes() != seed:
        raise RuntimeError("the initial edit changed across interruption and resume")
    if (
        receipts_before != (nonce + "\n").encode()
        or (workspace.repo / "corrections.txt").read_bytes() != receipts_before
    ):
        raise RuntimeError(
            "the correction was repeated or its receipt changed after resume"
        )
    if (workspace.repo / "verify_lifecycle.py").read_bytes() != verifier:
        raise RuntimeError("the worker changed the independent verifier")
    if (workspace.repo / "result.txt").read_text() != f"complete:{nonce}\n":
        raise RuntimeError("the final result does not match the project nonce")
    durable = json.loads(state_path.read_text())
    if (
        not durable.get("run_finished_at")
        or durable.get("active_turn")
        or durable.get("active_verification")
        or final["lock"]["active"]
        or durable.get("run_id") == first_state.get("run_id")
    ):
        raise RuntimeError("the final run has no quiescent durable finish")
    final_turns = durable.get("turns", [])
    if (
        len(final_turns) != 1
        or final_turns[0].get("session_id") != "2"
        or not final_turns[0].get("verification", {}).get("passed")
    ):
        raise RuntimeError(
            "the resumed run must verify session 2 without replaying session 1"
        )
    if (
        any(session["status"] != "complete" for session in final["sessions"])
        or len(final["sessions"]) != 2
    ):
        raise RuntimeError("both prepared sessions must be complete")
    return {
        "plan_dir": str(plan),
        "before": before,
        "stopped": midpoint,
        "final": final,
        "first_result": stopped,
        "resumed_result": finished,
        "redirect": journal,
        "delivery_count": deliveries,
        "first_run": first_state,
        "resumed_run": durable,
    }


def run_lifecycle(
    harness: HarnessName, *, opted_in: bool, deadline: int, keep: bool
) -> dict:
    started = time.monotonic()
    compat = harness_compatibility.check_harness(harness)
    result: dict[str, Any] = {
        "harness": harness,
        "executable": compat.executable,
        "version": compat.version,
        "compatibility": compat.status,
        "status": "skipped",
        "checks": [],
        "verified_version": None,
        "artifacts": None,
        "detail": "live acceptance was not enabled",
    }
    if opted_in and compat.executable and not compat.blocks:
        # Retain until the verdict is known; failed runs always keep their full
        # workspace, state, helper results, and each command's stdout/stderr.
        with disposable_workspace(
            f"{harness}-lifecycle", keep=True, announce=False
        ) as workspace:
            commands = Commands(workspace.root, deadline=deadline)
            try:
                commands.bound_harness(harness, compat.executable)
                result["evidence"] = run_sequence(workspace, commands, harness)
                after = harness_compatibility.check_harness(harness, use_cache=False)
                result["version_after"] = after.version
                if after.version is None or after.version != compat.version:
                    raise RuntimeError(
                        "harness version changed or became unavailable "
                        "during acceptance: "
                        f"{compat.version} -> {after.version}; no version is verified"
                    )
                result.update(
                    status="passed",
                    verified_version=compat.version,
                    detail="all six lifecycle checks passed",
                    checks=list(CHECKS),
                )
            except (Exception, KeyboardInterrupt) as error:
                result.update(
                    status="failed", detail=f"{type(error).__name__}: {error}"
                )
                result["interrupted"] = isinstance(error, KeyboardInterrupt)
            finally:
                result["cleanup"] = commands.cleanup()
                result["commands"] = commands.calls
                result["max_harness_launches"] = MAX_HARNESS_LAUNCHES
            result["artifacts"] = str(workspace.root)
            result["seconds"] = round(time.monotonic() - started, 3)
            (workspace.root / "acceptance.json").write_text(
                json.dumps(result, indent=2) + "\n"
            )
        if result["status"] == "passed" and not keep:
            shutil.rmtree(workspace.root)
            result["artifacts"] = None
    elif opted_in:
        result["detail"] = "harness is missing or its installed version is incompatible"
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def lifecycle_main(
    args: argparse.Namespace, *, requested: tuple[HarnessName, ...], opted_in: bool
) -> int:
    source = source_identity()
    with redirect_stdout(sys.stderr):
        results = []
        for name in requested:
            result = run_lifecycle(
                name,
                opted_in=opted_in,
                deadline=args.deadline,
                keep=args.keep_workspaces,
            )
            results.append(result)
            if result.get("interrupted"):
                break
    source_after = source_identity()
    unchanged = source == source_after
    report = {
        "schema_version": 1,
        "suite": "lifecycle",
        "requested_harnesses": list(requested),
        "opted_in": opted_in,
        "model_use": opted_in,
        "deadline_seconds_per_harness": args.deadline,
        "ok": unchanged and all(row["status"] != "failed" for row in results),
        "complete": unchanged and all(row["status"] == "passed" for row in results),
        "source": source,
        "source_after": source_after,
        "source_unchanged": unchanged,
        "harnesses": results,
        "verification_note": (
            "Only a passed complete lifecycle verifies its exact installed version; "
            "skipped runs provide no acceptance evidence."
        ),
    }
    if not unchanged:
        report["error"] = (
            "ASL sources changed during acceptance; rerun the final checkout"
        )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        Path(args.report).write_text(rendered)
    if args.json:
        print(rendered, end="")
    else:
        for row in results:
            print(
                f"{row['harness']} {row['version']}: {row['status']} "
                f"({row['seconds']:.1f}s) — {row['detail']}"
            )
            if row["artifacts"]:
                print(f"  artifacts: {row['artifacts']}")
        if not unchanged:
            print(report["error"], file=sys.stderr)
    return 0 if report["ok"] else 1
