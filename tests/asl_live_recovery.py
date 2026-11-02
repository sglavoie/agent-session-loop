"""Opt-in real-command crash, stale-lock recovery, and verified resume."""

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

from agent_session_loop import harness_compatibility, process_tree, state, state_locks
from agent_session_loop.harnesses import HarnessName
from tests.asl_live_lifecycle_process import (
    MAX_HARNESS_LAUNCHES,
    Commands,
    source_identity,
)
from tests.asl_live_recovery_process import start_run
from tests.asl_live_recovery_project import FROZEN, PRESERVED, write_project
from tests.asl_live_redirect_workspace import Workspace, disposable_workspace

CHECKS = (
    "prepare-and-preflight",
    "verified-session-before-crash",
    "runner-sigkill-during-tool",
    "stale-lock-unlocked-without-force",
    "predecessors-confirmed-stopped",
    "unconfirmed-history-requires-authorization",
    "completed-session-not-replayed",
    "partial-edits-preserved",
    "independent-verification",
    "quiescent-completion",
)


def verify_recovery_evidence(evidence: dict, harness: str) -> None:
    """Reject a successful process without proof of the actual crash and recovery."""
    if (
        not evidence.get("prepare", {}).get("ok")
        or not evidence.get("prepare", {}).get("marked_loop_ready")
        or not evidence.get("preflight", {}).get("ok")
    ):
        raise RuntimeError("real preparation and preflight must pass before the crash")
    before = evidence["before"]
    first = before.get("turns", [])
    active = before.get("active_turn") or {}
    final = evidence["final_run"]
    crashed = evidence["crashed_run"]
    turns = final.get("turns", [])
    if (
        len(first) != 1
        or first[0].get("session_id") != "1"
        or first[0].get("harness") != harness
        or first[0].get("returncode") != 0
        or not first[0].get("verification", {}).get("passed")
        or active.get("session_id") != "2"
        or before.get("run_finished_at")
        or crashed.get("run_id") != before.get("run_id")
        or crashed.get("turns") != first
        or (crashed.get("active_turn") or {}).get("session_id") != "2"
        or crashed.get("run_finished_at")
    ):
        raise RuntimeError(
            "crash must preserve verified session 1 and unfinished session 2"
        )
    process = evidence["processes"]
    if (
        process.get("signal") != "SIGKILL"
        or process.get("returncode") != -9
        or not process.get("descendants", {}).get("collected")
        or process.get("checkpoint", {}).get("pid")
        not in [row["pid"] for row in process.get("descendants", {}).get("entries", [])]
        or not process.get("confirmation", {}).get("complete")
        or not process.get("pre_resume_confirmation", {}).get("complete")
        or not evidence.get("final_process_confirmation", {}).get("complete")
    ):
        raise RuntimeError(
            "crash and stopped predecessor processes require positive evidence"
        )
    launches = evidence.get("harness_processes", [])
    if not 4 <= len(launches) <= MAX_HARNESS_LAUNCHES or any(
        not row.get("launch", {}).get("birth")
        or not row.get("confirmation", {}).get("complete")
        for row in launches
    ):
        raise RuntimeError(
            "every harness launch requires independent termination proof"
        )
    unlocked = evidence["unlock"]
    if (
        not evidence["stale_lock"].get("stale")
        or evidence["stale_lock"].get("pid") != process.get("runner_pid")
        or not unlocked.get("ok")
        or not unlocked.get("removed")
        or not unlocked.get("stale")
        or unlocked.get("active")
    ):
        raise RuntimeError("operator unlock must remove the real stale runner lock")
    refusal = evidence["resume_refusal"]
    conversation = refusal.get("details", {}).get("conversation_recovery", {})
    if (
        refusal.get("reason") != "conversation-recovery-required"
        or not refusal.get("paused")
        or conversation.get("selection_state") != "unconfirmed"
        or conversation.get("session_id") != "2"
        or evidence["refusal_launches_before"] != evidence["refusal_launches_after"]
        or "--fresh-conversation" not in evidence["resume_argv"]
    ):
        raise RuntimeError(
            "unconfirmed history requires an explicit fresh-conversation authorization"
        )
    if (
        not final.get("ok")
        or final.get("reason") != "all-complete"
        or not final.get("run_finished_at")
        or final.get("run_id") == before.get("run_id")
        or any(
            final.get(key)
            for key in ("active_turn", "active_verification", "active_finalization")
        )
        or evidence["final"]["lock"]["active"]
        or evidence["lock_present_after"]
    ):
        raise RuntimeError(
            "resumed run must finish durably with no active work or lock"
        )
    if (
        len(turns) != 1
        or turns[0].get("session_id") != "2"
        or turns[0].get("harness") != harness
        or turns[0].get("returncode") != 0
        or not turns[0].get("verification", {}).get("passed")
    ):
        raise RuntimeError(
            "resume must verify only session 2 without replaying session 1"
        )
    sessions = evidence["final"].get("sessions", [])
    if len(sessions) != 2 or any(row["status"] != "complete" for row in sessions):
        raise RuntimeError("both prepared sessions must be complete")
    started = [
        row["payload"]
        for row in evidence["events"]
        if row.get("name") == "turn_started"
    ]
    if [(row.get("run_id"), row.get("session_id")) for row in started] != [
        (before["run_id"], "1"),
        (before["run_id"], "2"),
        (final["run_id"], "2"),
    ]:
        raise RuntimeError(
            "durable turn history must show one session 1 and two session 2 attempts"
        )
    if not evidence.get("session1_handoff_unchanged"):
        raise RuntimeError("session 1 handoff changed after the crash")
    if set(evidence.get("preserved", {})) != set(PRESERVED) or not all(
        evidence["preserved"].values()
    ):
        raise RuntimeError("pre-crash edits changed across recovery")
    if set(evidence.get("frozen", {})) != set(FROZEN) or not all(
        evidence["frozen"].values()
    ):
        raise RuntimeError("worker changed frozen requirements or independent verifier")
    product = evidence.get("product_verification", {})
    if product.get("returncode") != 0 or not product.get("result", {}).get("ok"):
        raise RuntimeError("final independent product verification did not pass")


def run_sequence(
    workspace: Workspace, commands: Commands, harness: HarnessName
) -> dict:
    nonce = uuid.uuid4().hex
    source = write_project(workspace, nonce)
    frozen = {name: (workspace.repo / name).read_bytes() for name in FROZEN}
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
        "--max-sessions",
        "2",
        "--json",
    ]
    state_dir = state.plan_state_dir(plan, workspace.state_dir)
    state_path = state_dir / "state.json"
    lock_path = state_dir / "lock.json"
    with start_run(commands, run) as running:
        before = running.wait_for_checkpoint(
            state_path, workspace.repo / "sleeping.json", nonce
        )
        preserved = {name: (workspace.repo / name).read_bytes() for name in PRESERVED}
        handoff = (plan / "session-1.md").read_bytes()
        (workspace.root / "before-crash.json").write_text(
            json.dumps(before, indent=2) + "\n"
        )
        running.crash()
        crashed = json.loads(state_path.read_text())
        lock = state_locks.inspect_lock_info(plan, workspace.state_dir)
        if lock is None or not lock.stale or lock.pid != running.child.pid:
            raise RuntimeError("SIGKILL did not leave the expected stale runner lock")
        stale = {
            "pid": lock.pid,
            "stale": lock.stale,
            "reason": lock.reason,
            "raw": lock.raw,
        }
        (workspace.root / "stale-lock.json").write_text(
            json.dumps(stale, indent=2) + "\n"
        )
        running.stop_predecessors()
        unlocked = commands.asl("unlock", "unlock", *common)
        ledger = commands.root / "harness-launches.jsonl"
        refusal_launches_before = len(ledger.read_text().splitlines())
        refusal = commands.call(
            "resume-without-authorization",
            [*run, "--resume"],
            timeout=commands.remaining(),
            expected_returncode=1,
        )
        refusal_launches_after = len(ledger.read_text().splitlines())
        if refusal.get("reason") != "conversation-recovery-required":
            raise RuntimeError(f"unexpected recovery refusal: {refusal}")
        resume_argv = [*run, "--resume", "--fresh-conversation"]
        finished = commands.call(
            "resumed-run", resume_argv, timeout=commands.remaining()
        )
        final = commands.asl("final-status", "status", *common)
        durable = json.loads(state_path.read_text())
        records = [
            json.loads(line)
            for line in (state_dir / "events.jsonl").read_text().splitlines()
        ]
        frozen_after = {
            name: (workspace.repo / name).read_bytes() == content
            for name, content in frozen.items()
        }
        if not all(frozen_after.values()):
            raise RuntimeError(
                "worker changed frozen requirements or independent verifier"
            )
        product = commands.call(
            "independent-product-check",
            [sys.executable, str(workspace.repo / "verify_recovery.py"), "2", nonce],
        )
        harness_processes = []
        for line in ledger.read_text().splitlines():
            commands.remaining()
            entry = json.loads(line)
            confirmation = process_tree.confirm_termination(
                pgid=entry["pgid"],
                evidence={"collected": True, "entries": [entry]},
            )
            harness_processes.append({"launch": entry, "confirmation": confirmation})
        evidence = {
            "plan_dir": str(plan),
            "prepare": prepared,
            "preflight": preflight,
            "before": before,
            "crashed_run": crashed,
            "stale_lock": stale,
            "unlock": unlocked,
            "resume_refusal": refusal,
            "refusal_launches_before": refusal_launches_before,
            "refusal_launches_after": refusal_launches_after,
            "resume_argv": resume_argv,
            "resumed_result": finished,
            "final": final,
            "final_run": durable,
            "events": records,
            "processes": running.evidence,
            "harness_processes": harness_processes,
            "final_process_confirmation": running.confirm(),
            "lock_present_after": lock_path.exists(),
            "session1_handoff_unchanged": (plan / "session-1.md").read_bytes()
            == handoff,
            "preserved": {
                name: (workspace.repo / name).read_bytes() == content
                for name, content in preserved.items()
            },
            "frozen": frozen_after,
            "product_verification": {
                "result": product,
                "returncode": commands.calls[-1]["returncode"],
            },
        }
        verify_recovery_evidence(evidence, harness)
        return evidence


def run_recovery(
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
    if opted_in and compat.executable and compat.version and not compat.blocks:
        with disposable_workspace(
            f"{harness}-recovery", keep=True, announce=False
        ) as workspace:
            commands = Commands(workspace.root, deadline=deadline)
            try:
                commands.bound_harness(harness, compat.executable, track_processes=True)
                result["evidence"] = run_sequence(workspace, commands, harness)
                after = harness_compatibility.check_harness(harness, use_cache=False)
                result["version_after"] = after.version
                if after.version is None or after.version != compat.version:
                    raise RuntimeError(
                        "harness version changed or became unavailable; "
                        "no version is verified"
                    )
                result.update(
                    status="passed",
                    verified_version=compat.version,
                    checks=list(CHECKS),
                    detail="all crash recovery checks passed",
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
        result["detail"] = "harness is missing, unversioned, or incompatible"
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def recovery_main(
    args: argparse.Namespace, *, requested: tuple[HarnessName, ...], opted_in: bool
) -> int:
    source = source_identity()
    with redirect_stdout(sys.stderr):
        results = []
        for name in requested:
            result = run_recovery(
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
        "suite": "recovery",
        "requested_harnesses": list(requested),
        "opted_in": opted_in,
        "model_use": opted_in,
        "deadline_seconds_per_harness": args.deadline,
        "ok": unchanged and all(row["status"] != "failed" for row in results),
        "complete": unchanged
        and len(results) == len(requested)
        and all(row["status"] == "passed" for row in results),
        "source": source,
        "source_after": source_after,
        "source_unchanged": unchanged,
        "harnesses": results,
        "verification_note": (
            "Only a complete passed crash/recovery sequence verifies its exact "
            "harness version. Skipped probes provide no acceptance evidence. "
            "Orphan processes are stopped explicitly by the operator fixture, "
            "not automatically by a dead runner."
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
                f"{row['harness']} {row['version']}: {row['status']} — {row['detail']}"
            )
            if row["artifacts"]:
                print(f"  artifacts: {row['artifacts']}")
        if not unchanged:
            print(report["error"], file=sys.stderr)
    return 0 if report["ok"] else 1
